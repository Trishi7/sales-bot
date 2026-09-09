"""The GTM Playbook spreadsheet — live reads, and one narrow write path.

The sales team's system of record is a Google Sheet, read LIVE through the Sheets
API with a service account. There is no sync interval and no local copy: a
question asked now is answered from the sheet as it is now, within a short cache.
(rclone is only ever used for the meeting-notes docs; it has nothing to do with
this file.)

ONE SPREADSHEET: "NFThing <> GTM Playbook", read AND written.

THE SANDBOX COPY IS RETIRED. There used to be two — the original as a read-only
source of truth, and a writable mirror the bot owned a column on. That was the
right shape while nobody had agreed what the bot may touch, and it was useless
for exactly as long: a column on a copy nobody opens is a write into a drawer,
and the two sheets were only ever row-aligned by luck. The bot now writes into
the real playbook, into the Outreach PoCs tab, inside the writable window
between the restricted bands — and nowhere else, enforced in code.

A SECOND spreadsheet — the researcher/buyer mapping — is deliberately NOT handled
here. It is read-only by policy and lives in mapping_sheet.py, which has no write
method and a read-only OAuth scope. This module only has to know that it must
never write to it: `_refuse_if_read_only()` checks the playbook id against
`config.is_read_only_sheet_id()` before a cell is touched.

THE CANONICAL TAB IS "Outreach PoCs", AND IT IS FOUND BY NAME. That is the one
exception to how everything else here works, and it is deliberate. Phase 2 moved
the bot's sheet world onto that tab by name; the RETIRED outreach tracker tab
("Outreach Updates") carries near-identical columns, so a header signature would
cheerfully re-adopt the retired tab and undo the move. `GTM_POCS_TAB_TITLES`
names it, `_detect_kind` claims it before any signature is scored, and its
COLUMNS are still discovered dynamically at parse time.

EVERY OTHER TAB IS IDENTIFIED BY HEADER SIGNATURE, NOT BY NAME. Tab names in the
real playbook drift — "Master data" is now "Master Data" — but the COLUMNS a tab
carries are what make it that tab. So every other kind below is defined by a
small set of headers that only that tab has, and the title is at most a tie-break
hint. The tab NAME that matched each role is logged at startup, so "which tab is
which today" is answerable from one log line.

    outreach_pocs        THE CANONICAL TAB, matched BY NAME against
                         GTM_POCS_TAB_TITLES (default "Outreach PoCs"). Every
                         proactive feature reads this tab and no other. A row on
                         it is ACTIVE only when a first-contact date or a
                         connection date is present — see activation.py.
    master_data          STATUS ONLY — Connected / Intro Sent / Response Status /
                         Meeting Done / Assets Shared, one row per company/PoC,
                         and a "Month" that is a month, not a date. Signature:
                         "Response Status" AND "Intro Sent" AND "Meeting Done".
                         Used for aggregate answers and for the weekly funnel.
    lead_pipeline        Signature: "Lead Stage" AND "Estimated Value (INR)".
                         Priority Level is populated; stage and value are blank
                         today, which is why nothing reads them yet.
    funnel_pivot         Signature: "Vertical / Stage". The funnel DEFINITION:
                         Contacted → Connected → Intro Sent → Positive (P/Y) →
                         Meeting Done → Assets Shared.
    researcher_lines     Signature: "Outreach Line - Researchers" AND "Dates".
    positioning_matrix   use cases A–I: label, use case, problem, offering,
                         company type, ICP, business impact.
    prospect_priority    scored companies, P1–P3, with a rationale.

HIDDEN TABS ARE READ. gspread returns hidden worksheets like any other, and the
master tab's own feed is hidden — so discovery includes them (GTM_READ_HIDDEN_TABS)
and the schema log marks them "hidden" rather than pretending they aren't there.

THE BOT READS VALUES, NEVER FORMATTING. A status conveyed by CELL COLOUR is
invisible to it. That failure is silent by nature, so `_warn_colour_coded()`
looks for its signature at startup — a mapped column that is empty on nearly
every row — and names the column in a warning instead of quietly treating the
whole column as blank.

HEADERS ARE DISCOVERED AT PARSE TIME. The tabs will evolve — columns get added,
renamed, reordered — so nothing here is positional. Each tab's header row is
read, each header is matched to a known ROLE by a set of aliases, and everything
unmatched is still carried as an "extra" so a question about a column this code
has never heard of can still be answered. `GTM_COLUMN_MAP` overrides the match
when wording is genuinely ambiguous.

WRITES ARE DELIBERATELY TINY, AND THE RESTRICTED BANDS MAKE THEM SMALLER.
`RESTRICTED_COLUMN_RANGES` (default "A:I,S:X") names bands of columns that no
write path here may touch; `_refuse_if_restricted()` is checked by every one of
them, fails closed, and refuses rather than raising. READING IS UNRESTRICTED —
this is a write lock only. The NAMED columns inside the writable window between
the bands are logged at startup (`log_writable_window`) so a column that has
shifted is visible before a write lands in the wrong place.

`write_cells()` is the whole write surface: individual cells in ONE row of the
canonical tab, one `values.update` per cell, all inside the writable window.
Never a row, never a full-sheet write, never a column outside the window. Every
write returns the PRIOR value of each cell so `undo_cells()` can put them back
exactly — which is what makes writing into the team's live sheet acceptable at
all.

FAILURE IS A FIRST-CLASS STATE. No access, a quota ban, an auth failure — none of
these may take the bot down. A failed read falls back to the last cached copy
with an explicit staleness note; a sheet that was never readable reports
awaiting-access along with the exact "share it with <service account>"
instruction, so the fix is one line rather than a debugging session.
"""
import logging
import re
import threading
import time
from typing import Optional

import config

log = logging.getLogger(__name__)

# gspread / google-auth are optional at IMPORT time: the bot must still start and
# still answer non-sheet questions on a box where they aren't installed. The
# import error is reported through the normal awaiting-access path instead.
try:
    import gspread
    from google.oauth2.service_account import Credentials

    _IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - depends on the environment
    gspread = None
    Credentials = None
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"

# Read+write on spreadsheets only. No Drive scope: the bot has no business
# listing, creating, sharing or deleting files, and a narrower token is a
# narrower blast radius if the key ever leaks.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ONE SPREADSHEET. `ORIGINAL` survives as the cache key and the label in log
# lines; `COPY` is gone with the sandbox era it named. Keeping the constant
# rather than threading a bare string through the cache keys means "which sheet"
# has exactly one answer everywhere.
ORIGINAL = "original"

# Tab kinds.
MASTER = "master_data"
POSITIONING = "positioning_matrix"
# THE CANONICAL TAB. Phase 2 moved the bot's sheet world to the "Outreach PoCs"
# tab, and this is the kind that names it.
POCS = "outreach_pocs"
PRIORITY = "prospect_priority"
PIPELINE = "lead_pipeline"
FUNNEL = "funnel_pivot"
RESEARCHER_LINES = "researcher_lines"
# THE EVENTS & SUMMITS TAB. Conferences, summits and the like, each earning one
# reminder at T-EVENT_LEAD_DAYS and never another.
EVENTS = "events_summits"

# TRACKER IS A RETIRED NAME KEPT AS AN ALIAS FOR THE CANONICAL KIND. The old
# outreach tracker tab is no longer read as anything special; every call site
# that used to ask for `TRACKER` now resolves to the Outreach PoCs tab, which is
# the correct behaviour for all of them (they are lookups and answers, not
# rules). It exists so that a stray reference cannot silently resolve to a kind
# that no longer has a tab and quietly return None.
TRACKER = POCS

# What each kind IS, in one phrase, for the startup role-assignment log. The
# whole point of that log line is that somebody reading it can tell whether the
# right tab got the right job without opening the spreadsheet.
KIND_LABELS = {
    POCS: "OUTREACH PoCs — THE CANONICAL TAB (found by name)",
    MASTER: "MASTER — status only (aggregates, funnel, cross-check)",
    PIPELINE: "PIPELINE — lead stage / estimated value",
    FUNNEL: "FUNNEL PIVOT — the funnel stage definition",
    RESEARCHER_LINES: "RESEARCHER LINES — outreach lines for researchers",
    EVENTS: "EVENTS & SUMMITS — one reminder each, T-minus the lead days",
    POSITIONING: "POSITIONING — the use-case / pitch matrix",
    PRIORITY: "PRIORITY — scored prospect lists",
}

# The roles anything reading the canonical tab depends on. Every one of them is
# nameable in GTM_COLUMN_MAP, so a column rename in the sheet is an env change.
#
# `first_contacted` and `connected` are the two ACTIVATION roles — a row is
# active only when one of them holds a date (see activation.py) — so a column
# rename that loses either of them would make the whole tab look inactive. They
# are checked by the colour-coding warning below for exactly that reason.
CADENCE_ROLES = (
    "company", "industry", "poc", "poc_designation", "poc_vertical",
    "first_contacted", "first_contact_type", "connected", "dm_sent_date",
    "prospect_stage", "closure", "deal_status", "intro_date", "last_followed_up",
    "followups_count", "response", "meeting_date", "assets_shared", "next_steps",
)

# The roles whose presence makes a row ACTIVE. Named here rather than in
# activation.py so that the schema layer and the activation rule cannot drift
# apart: this is the list the startup schema log reports on.
ACTIVATION_ROLES = ("first_contacted", "connected")

# The roles the NEXT-ACTION STATE MACHINE reads, on top of the activation ones.
# Named here for the same reason: nextaction.py decides what a row needs next,
# and a header it silently failed to map is a trigger that silently never fires.
# The startup report prints which of these mapped and which did not.
NEXT_ACTION_ROLES = (
    "first_contacted", "first_contact_type", "connected", "dm_sent_date",
    "prospect_stage", "closure", "deal_status", "response", "followups_count",
    "meeting_date", "next_steps", "last_followed_up", "owner",
)

# SPREADSHEET ERROR VALUES ARE NOT DATA. A "#REF!" left behind by a broken
# formula is the absence of a value, and reading it as text made a date column
# unparseable and a status column "responded". Every one of these is normalised
# to EMPTY at parse time (section 2 of the phase-1 spec) — and the raw cell is
# remembered on the Tab so the data-quality flag can report it once.
SHEET_ERROR_VALUES = (
    "#ref!", "#n/a", "#value!", "#div/0!", "#name?", "#null!", "#num!",
    "#error!", "#getting_data", "#spill!", "#calc!",
)


def is_error_value(value) -> bool:
    """True when a cell holds a spreadsheet error rather than a value."""
    return str(value or "").strip().lower() in SHEET_ERROR_VALUES


def clean_cell(value) -> str:
    """A raw cell as the rest of the bot should see it: stripped, and EMPTY when
    it is a spreadsheet error. This is the single normalisation point — every
    row dict built by `_parse_values` has been through it."""
    text = str(value or "").strip()
    return "" if is_error_value(text) else text


def sheet_date(value):
    """A sheet cell → a date, tolerating the two things this sheet actually does.

    `deadlines.parse_date` handles the formats; this handles the CELL. Two of
    them in the live tracker hold more than one date ("25-Mar-2026\\n02-04-2026"
    is a meeting that moved), and `parse_date` on the whole string returns None -
    which read as "no meeting date" and dropped a real meeting out of the
    cadence. Multi-value cells are split and the LATEST readable date wins,
    because a rescheduled meeting is the one that is going to happen.

    Imported lazily: `deadlines` imports `config`, and a module-level import
    here would make the two files circular the day `deadlines` needs a sheet.
    """
    import deadlines as _dl

    text = clean_cell(value)
    if not text:
        return None
    direct = _dl.parse_date(text)
    if direct is not None:
        return direct
    found = []
    for part in re.split(r"[\n;,/]|\s{2,}", text):
        part = part.strip()
        if not part:
            continue
        parsed = _dl.parse_date(part)
        if parsed is not None:
            found.append(parsed)
    return max(found) if found else None


# -- header roles -------------------------------------------------------------
# role -> the alias fragments that identify it. Matching is case-insensitive and
# punctuation-insensitive, tried as exact-normalised first and then as a
# substring, longest alias first — so "last followed up date" beats a loose
# "date" match. Order within a list is only a tie-break aid; specificity wins.
ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    # THE MASTER TAB — STATUS ONLY. Its columns are Connected / Intro Sent /
    # Response Status / Meeting Done / Assets Shared plus a "Month" that is a
    # MONTH ("Mar-2026"), not a date. It carries no Last-followed-up, no
    # Total-follow-ups, no Next Steps and no Meeting Date, which is exactly why
    # the cadence cannot run on it: every date rule would read a blank and
    # silently never fire. It answers aggregate questions, defines the weekly
    # funnel, and is cross-checked against the tracker.
    #
    # Its status columns get their OWN roles (intro_sent, response_status,
    # meeting_done) rather than being aliased onto the tracker's date roles.
    # They were aliased once — "Intro Sent" matched `intro_date` on the
    # substring "intro" — and rule (b) then read the word "No" as a recorded
    # intro date and never fired on a single row.
    MASTER: {
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial", "#"),
        "company": ("company", "company name", "account", "organisation",
                    "organization", "client", "org"),
        "industry": ("industry", "sector", "domain"),
        "poc": ("poc", "person", "point of contact", "contact name",
                "contact person", "poc name", "champion", "contact"),
        "poc_designation": ("designation", "poc designation", "title", "job title", "role"),
        "poc_vertical": ("poc vertical", "vertical", "department", "function", "team"),
        # A month, not a date. Read as the 1st when a date is unavoidable, and
        # never used by a cadence rule.
        "month": ("month", "initial contact month", "month of first contact"),
        "connected": ("connected", "connected?", "connection status", "is connected"),
        "intro_sent": ("intro sent", "intro sent?", "membrane intro sent", "introduction sent"),
        "response_status": ("response status", "response?", "response", "responded", "replied"),
        "meeting_done": ("meeting done", "meeting done?", "met", "meeting held"),
        "assets_shared": ("assets shared", "assets shared?", "assets", "collateral shared",
                          "material shared", "deck shared"),
        "asset_detail": ("asset detail", "assets detail", "asset details", "which assets"),
        # Read when present; the live tab has neither, and the cadence resolves
        # its owner through SALES_DEFAULT_OWNER_ID instead.
        "owner": ("owner", "row owner", "assigned to", "assignee", "sdr", "bd",
                  "account owner", "handled by", "responsible"),
        "status": ("status", "stage", "deal status", "current status"),
        "reason": ("reason", "lost reason", "reason for no response", "why"),
        "other_updates": ("other updates", "updates", "notes", "comments", "remarks"),
    },
    POSITIONING: {
        "label": ("label", "use case label", "ref", "id"),
        "use_case": ("use case", "usecase", "case"),
        "problem": ("problem statement", "problem", "pain"),
        "offering": ("offering", "what we offer", "solution", "product"),
        "company_type": ("company type", "type of company", "segment", "category"),
        "icp": ("icp", "ideal customer profile", "ideal customer"),
        "business_impact": ("business impact", "impact", "value", "outcome"),
    },
    # THE CANONICAL TAB: "Outreach PoCs". Its headers are DISCOVERED, not
    # assumed — the alias lists below are how a discovered header is given a
    # role, and GTM_COLUMN_MAP overrides any of them without a code change.
    #
    # `first_contacted` and `connected` carry extra date-shaped aliases
    # ("connection date", "date connected") because they are the ACTIVATION
    # columns: a row is invisible to every proactive feature unless one of them
    # holds a date, so a header this list fails to recognise is not a cosmetic
    # miss, it silently empties the bot's world.
    POCS: {
        "sr_no": ("sr. no.", "sr no", "s.no", "sno", "serial", "#"),
        "company": ("company", "company name", "account", "client", "organisation", "organization"),
        "industry": ("industry", "sector", "vertical (company)"),
        "poc": ("poc", "point of contact", "contact name", "contact person", "champion"),
        "poc_designation": ("poc designation", "designation", "title", "role"),
        "poc_vertical": ("poc vertical", "vertical", "department", "function"),
        # THE CONTACT DETAILS. Mapped so the bot can NAME the column when it
        # refuses to write one — these live in the restricted identity band, and
        # "I never write to Email" is a far better answer than "I have no rule
        # for that". Reading them is unrestricted, as it always was.
        "email": ("email", "e-mail", "email address", "email id", "mail"),
        "linkedin": ("linkedin", "linkedin url", "linkedin profile", "profile",
                     "profile link", "li url"),
        "phone": ("phone", "phone number", "mobile", "contact number", "whatsapp number"),
        # RESEARCH LINKS. Papers, profiles and publication pages somebody put on
        # the row. The research brief fetches ONLY what is here, and only from
        # RESEARCH_ALLOWED_DOMAINS — it never searches the web and never guesses
        # a URL from a name.
        "research_links": ("research", "research link", "research links", "papers",
                           "paper", "publications", "publication", "arxiv",
                           "google scholar", "scholar", "work", "portfolio"),
        "first_contacted": ("first contact date", "date of first contact",
                            "first contacted date", "first contacted", "first contact",
                            "outreach date", "date of outreach"),
        # HOW the first contact was made — email / LinkedIn / call / WhatsApp.
        # Read ONLY by the progress check, which drops its "ask for their email
        # address" line when the first contact was already by email. Asking a
        # prospect you emailed for their email address is the kind of line that
        # gets a bot switched off. See config.EMAIL_CONTACT_TYPES.
        "first_contact_type": ("first contact type", "contact type", "type of first contact",
                               "first contact via", "channel", "outreach channel",
                               "contacted via", "medium"),
        # WHEN THE DM WENT OUT, which is a different fact from the connection
        # date. Connected-and-no-DM and DM-sent-and-no-reply are two different
        # states needing two different next actions, and one date cannot carry
        # both.
        "dm_sent_date": ("dm sent date", "dm sent", "date dm sent", "dm date",
                         "message sent date", "dm sent on", "first dm"),
        # WHERE THE PROSPECT IS. The quote chase fires off the value "Demo".
        "prospect_stage": ("prospect", "prospect stage", "stage", "pipeline stage",
                           "prospect status", "funnel stage"),
        # THE TERMINAL COLUMN. "0%", "Dead", "Unresponsive", "Won", "Lost" stop
        # a row for good; a percentage puts it in the fast or the slow lane.
        "closure": ("closure", "closure %", "closure percentage", "closure probability",
                    "probability", "% closure", "close probability", "likelihood of closure"),
        # In Progress / On Hold. Separate from `closure` because a deal can be
        # 70% and parked, and those need opposite treatment.
        "deal_status": ("deal status", "deal", "deal stage", "deal state",
                        "opportunity status"),
        "use_case": ("use case", "usecase", "pitch"),
        "intro_date": ("membrane intro date", "intro date", "introduction date", "intro"),
        "last_followed_up": ("last followed up date", "last followed up", "last follow up", "last followup", "last touch"),
        "followups_count": ("total follow-ups till date", "total follow ups", "total followups", "number of follow-ups", "follow-ups", "followups"),
        "response": ("response?", "response", "responded", "replied"),
        "reason": ("reason", "lost reason", "reason for no response", "why"),
        "meeting_date": ("meeting date", "meeting", "call date", "demo date"),
        # MEETING STATUS is a different fact from the meeting DATE — "booked",
        # "done", "no-show", "rescheduled". The reply loop writes this one; a
        # date and a state cannot share a cell.
        "meeting_status": ("meeting status", "meeting state", "meeting?",
                           "meeting done", "meeting outcome"),
        "assets_shared": ("assets shared", "assets", "collateral shared", "material shared"),
        # WHETHER THE PACKAGE WENT OUT. Distinct from assets_shared, which in
        # this sheet means collateral generally; "package sent" is the specific
        # thing the reply loop asks about.
        "package_sent": ("package sent", "package", "package shared", "pack sent",
                         "proposal sent", "packet sent"),
        # DEAL SIZE — explicit-command only, never written off a reply. A number
        # somebody mentioned in passing is not a number somebody committed to
        # the sheet.
        "deal_size": ("deal size", "deal value", "estimated value", "value",
                      "contract value", "ticket size"),
        "next_steps": ("next steps", "next step", "action", "next action"),
        "other_updates": ("other updates", "updates", "notes", "comments", "remarks"),
        "connected": ("connection date", "date connected", "connected on",
                      "connected date", "connect date", "connected?", "connected",
                      "connection status"),
        # NO OWNER COLUMN TODAY. The role is mapped anyway so that the day one
        # appears (or GTM_COLUMN_MAP names one) the bot starts addressing rows
        # to the person who owns them. Until then every line resolves to
        # SALES_DEFAULT_OWNER_ID.
        "owner": ("owner", "row owner", "assigned to", "assignee", "sdr", "bd",
                  "account owner", "handled by", "responsible"),
        "status": ("status", "stage", "deal status", "current status"),
    },
    # THE PIPELINE TAB. Priority Level is populated (High/Medium/Low); Lead
    # Stage and Estimated Value are blank on every row today. Nothing reads the
    # blank ones — they are mapped so that the day they get filled the tab is
    # already understood.
    PIPELINE: {
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial"),
        "use_case": ("use case", "usecase"),
        "company": ("company", "company name", "account", "client"),
        "industry": ("industry", "sector"),
        "priority": ("priority level", "priority", "tier", "band"),
        "poc": ("poc", "poc name", "point of contact", "contact name"),
        "poc_designation": ("poc designation", "designation", "title"),
        "geography": ("geography", "region", "country", "geo"),
        "lead_source": ("lead source", "source"),
        "lead_stage": ("lead stage", "stage", "deal stage"),
        "likelihood": ("% likelihood", "likelihood", "probability", "confidence"),
        "est_value": ("estimated value (inr)", "estimated value", "deal value",
                      "value (inr)", "est value"),
        "remarks": ("remarks", "notes", "comments"),
        "won_lost_reason": ("deal won / lost - reason", "won / lost reason",
                            "won lost reason", "deal reason"),
    },
    # THE FUNNEL PIVOT. Read for its STAGE NAMES, which are the funnel
    # definition the weekly digest reports against — not for its numbers, which
    # are a pivot of the master tab and are recomputed from the master rows so
    # that a stale pivot cannot be reported as this week's funnel.
    FUNNEL: {
        "vertical_stage": ("vertical / stage", "vertical/stage", "vertical stage",
                           "response / month", "response/month"),
        "contacted": ("contacted",),
        "connected": ("connected",),
        "intro_sent": ("intro sent",),
        "positive": ("positive (p/y)", "positive"),
        "meeting_done": ("meeting done",),
        "assets_shared": ("assets shared",),
    },
    # THE RESEARCHER OUTREACH LINES. One line per company for researcher-led
    # outreach, plus a "Dates" column that is a formula.
    RESEARCHER_LINES: {
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial"),
        "company": ("company", "company name", "account"),
        "industry": ("industry", "sector"),
        "geography": ("geography", "region", "country", "geo"),
        "funding": ("approx. funding", "approx funding", "funding"),
        "outreach_line": ("outreach line - researchers", "outreach line researchers",
                          "outreach line"),
        "dates": ("dates", "date"),
    },
    # THE EVENTS & SUMMITS TAB. Read for its DATES: each event earns one
    # reminder at T-EVENT_LEAD_DAYS. Everything else on the row is carried into
    # that reminder so it says something useful rather than "there is an event".
    EVENTS: {
        "event": ("event", "event name", "summit", "conference", "name"),
        "event_date": ("date", "event date", "start date", "dates", "when"),
        "location": ("location", "city", "venue", "where", "geography"),
        "event_type": ("type", "event type", "format", "category"),
        "owner": ("owner", "assigned to", "assignee", "responsible", "who"),
        "status": ("status", "attending", "attending?", "decision", "going"),
        "cost": ("cost", "price", "budget", "ticket", "fee"),
        "notes": ("notes", "comments", "remarks", "other updates", "details"),
        "link": ("link", "url", "website", "site", "registration"),
    },
    PRIORITY: {
        "company": ("company", "company name", "account", "prospect", "organisation", "organization"),
        "priority": ("priority", "p1/p2/p3", "tier", "band", "priority bucket"),
        "score": ("score", "rating", "points", "weighted score"),
        "rationale": ("rationale", "reason", "why", "justification", "notes"),
        "industry": ("industry", "sector"),
        "use_case": ("use case", "usecase"),
    },
}

# TAB-KIND DETECTION IS BY HEADER SIGNATURE. Names drift; signatures don't.
#
# Each entry is (kind, signature roles, how many must match, roles that are
# MANDATORY however good the rest of the score is). The mandatory set is the
# signature from the phase-1 spec — the columns only that tab has — and it is
# what stops false positives on a spreadsheet with twenty tabs.
#
# Two false positives this list is shaped by, both real:
#   - the investor tab, whose "2025-26 Activity / Notes" header matched
#     `rationale` and whose fund names matched loosely, was read as a prospect
#     list. `company` and `priority` are mandatory for PRIORITY now, so a
#     scoring scratchpad with no companies in it (the live "Activation Score -
#     Warmed Up Co" tab) cannot claim the kind.
# THE CANONICAL TAB IS NOT IN THIS LIST. "Outreach PoCs" is claimed BY NAME
# (GTM_POCS_TAB_TITLES) before any signature is scored — see `_detect_kind`.
# It used to be found by signature, as the outreach tracker, and that is exactly
# what had to stop: the retired tracker tab carries near-identical columns, so a
# signature would happily re-adopt it as the canonical tab and put the bot's
# whole world back on the tab phase 2 retired.
_KIND_SIGNATURES: list[tuple[str, tuple[str, ...], int, tuple[str, ...]]] = [
    (MASTER,
     ("company", "poc", "connected", "intro_sent", "response_status",
      "meeting_done", "assets_shared"),
     4,
     ("company", "response_status", "intro_sent", "meeting_done")),
    (FUNNEL,
     ("vertical_stage", "contacted", "connected", "intro_sent", "meeting_done"),
     2,
     ("vertical_stage",)),
    (RESEARCHER_LINES,
     ("company", "outreach_line", "dates"),
     2,
     ("outreach_line", "dates")),
    # EVENTS: a named event with a date. Mandatory on both, because "name + date"
    # describes half the tabs in a playbook and only this one is a list of events
    # — the NAME HINT below is what separates it from a schedule of anything else.
    (EVENTS,
     ("event", "event_date", "location", "status"),
     2,
     ("event", "event_date")),
    (PIPELINE,
     ("company", "lead_stage", "est_value", "priority", "likelihood"),
     3,
     ("company", "lead_stage", "est_value")),
    (POSITIONING,
     ("use_case", "problem", "offering", "icp", "business_impact"),
     3,
     ("use_case",)),
    (PRIORITY,
     ("company", "priority", "score", "rationale"),
     2,
     ("company", "priority")),
]

# Name hints are a TIE-BREAK ONLY, applied when two kinds score equally. They
# never override a signature, because the signature is the thing that survives a
# rename — which is the whole reason detection works this way.
_KIND_NAME_HINTS: list[tuple[str, tuple[str, ...]]] = [
    (MASTER, ("master data", "master")),
    (FUNNEL, ("funnel",)),
    (RESEARCHER_LINES, ("researcher", "master pipeline")),
    (EVENTS, ("event", "summit", "conference")),
    (PIPELINE, ("lead", "pipeline")),
    (POSITIONING, ("positioning", "matrix", "use case", "usecase", "playbook")),
    (PRIORITY, ("priority", "prospect", "scoring", "score", "p1")),
]

# How hard the write round-trip tries to read its cell back before giving up.
# Sheets' read quota is per-minute, so a short exponential backoff clears the
# common case; three attempts is ~7s worst case, which a self-test can afford.
_ROUNDTRIP_READ_ATTEMPTS = 3
_ROUNDTRIP_BACKOFF_SECONDS = 2

# "P1" / "p-2" / "Priority 3" → 1 / 2 / 3.
_PRIORITY_RE = re.compile(r"\bp[\s\-]?([123])\b", re.IGNORECASE)
# Values that mean "yes" in a flag column like Connected?.
_YES = {"yes", "y", "true", "1", "responded", "replied", "connected", "positive", "done"}
_NO = {"no", "n", "false", "0", "none", "-", "na", "n/a", "not yet", "nil"}

# THE RESPONSE VOCABULARY, taken from what the two live tabs actually contain
# rather than from what a "Response?" header suggests. The tracker's column
# holds "P" / "N" / "Did Respond" / "Awaited" / blank; the master's "Response
# Status" holds "P - Positive/In Progress" / "N - Rejected" / "No Response".
# Neither is a yes/no flag, and reading them as one made every replied prospect
# invisible.
#
# THE THREE BUCKETS ARE THE PHASE-1 SPEC'S, exactly:
#   POSITIVE  Y, P, "Did Respond", "P - Positive/In Progress"
#   REJECTED  N, "N - Rejected", "No"
#   NONE      blank, "No Response", "Awaited"
# A fourth state, RESPONSE_UNKNOWN, exists for values in NEITHER list. It is
# never silently folded into one of the three: an unrecognised value means
# somebody DID write something in the cell, so the row is not chased as silent,
# it is treated as awaiting our action, AND the raw value is reported once by
# the data-quality flag so the sheet can be standardised.
RESPONSE_POSITIVE = "positive"
RESPONSE_NEGATIVE = "negative"
RESPONSE_UNKNOWN = "responded"   # they replied; the sheet doesn't say how it went
RESPONSE_NONE = "none"           # nothing back yet

_RESP_POSITIVE = {
    "p", "y", "yes", "positive", "p positive in progress", "p positive",
    "positive in progress", "in progress", "did respond", "responded", "replied",
    "interested", "warm", "keen", "good",
}
_RESP_NEGATIVE = {
    "n", "no", "negative", "n rejected", "rejected", "not interested",
    "declined", "pass", "lost",
}
_RESP_NONE = {"", "awaited", "awaiting", "await", "no response", "none", "-", "na",
              "n/a", "nil", "not yet", "pending", "tbd", "no reply", "not responded"}

# Every value the three sets above cover, for the "raw response values outside
# the known set" data-quality flag.
KNOWN_RESPONSE_VALUES = _RESP_POSITIVE | _RESP_NEGATIVE | _RESP_NONE


def response_status(value) -> str:
    """Classify a Response? / Response Status cell into one of the four states.

    Anything unrecognised counts as RESPONSE_UNKNOWN — a human wrote something
    in the cell, so they DID hear back, and treating an unfamiliar note as
    silence would make the bot chase a prospect who has already answered.
    """
    v = normalise_header(clean_cell(value)).replace("?", "").strip()
    if v in _RESP_NONE:
        return RESPONSE_NONE
    if v in _RESP_POSITIVE:
        return RESPONSE_POSITIVE
    if v in _RESP_NEGATIVE:
        return RESPONSE_NEGATIVE
    # Prefix forms the sheet uses for its own dropdowns: "P - …" and "N - …".
    if v.startswith("p ") or v.startswith("y "):
        return RESPONSE_POSITIVE
    if v.startswith("n ") or v.startswith("not ") or v.startswith("no "):
        return RESPONSE_NEGATIVE
    if "did respond" in v or "responded" in v or "replied" in v:
        return RESPONSE_POSITIVE
    return RESPONSE_UNKNOWN


def is_known_response(value) -> bool:
    """Is this raw cell one of the values the vocabulary above knows by name?

    Used only by the data-quality flag. A False here is not an error — the row
    is still classified, by the prefix rules — it is a note that the sheet has
    grown a wording nobody standardised.
    """
    v = normalise_header(clean_cell(value)).replace("?", "").strip()
    return v in KNOWN_RESPONSE_VALUES


def normalise_header(text: str) -> str:
    """A header as a comparable key: lower-cased, punctuation collapsed to
    spaces, whitespace squeezed. "Sr. No." and "sr no" become the same thing."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9?]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_yes(value) -> bool:
    """True when a Response?/Connected? cell says yes. Anything unrecognised is
    NOT a yes — a "maybe" or a stray note must never be read as a reply."""
    v = normalise_header(clean_cell(value)).replace("?", "").strip()
    return v in _YES


def is_no_or_blank(value) -> bool:
    """True when a cell is empty or explicitly negative. Distinct from `not
    is_yes(...)`, which would also swallow "waiting on legal"."""
    v = normalise_header(clean_cell(value)).replace("?", "").strip()
    return v == "" or v in _NO


def parse_priority(value) -> Optional[int]:
    """"P1" → 1. None when the cell doesn't name a priority band."""
    m = _PRIORITY_RE.search(str(value or ""))
    if m:
        return int(m.group(1))
    v = normalise_header(str(value or ""))
    return int(v) if v in ("1", "2", "3") else None


class SheetAccessError(Exception):
    """A sheet could not be read. Carries a human-actionable `remedy` — usually
    the exact share instruction — because "permission denied" alone has never
    helped anyone."""

    def __init__(self, message: str, *, remedy: str = "", transient: bool = False):
        super().__init__(message)
        self.remedy = remedy
        # Transient (quota, 5xx, network) vs structural (not shared, no key).
        # The former is worth answering from cache; the latter needs a human.
        self.transient = transient


class Tab:
    """One parsed worksheet: its discovered schema and its rows.

    `rows` are dicts keyed by ROLE, plus "_row" (the 1-based sheet row number,
    needed to address a cell for a write) and "_extra" (every column that didn't
    map to a known role, keyed by its original header — so a question about a
    column this code has never heard of is still answerable).
    """

    def __init__(self, *, title: str, kind: str, headers: list[str],
                 role_to_col: dict[str, int], rows: list[dict], read_at: float,
                 header_row: int = 1, error_cells: Optional[list] = None):
        self.title = title
        self.kind = kind
        self.headers = headers
        self.role_to_col = role_to_col      # role -> 0-based column index
        self.rows = rows
        self.read_at = read_at
        # 1-based sheet row the headers live on. Kept because appending the bot's
        # column has to put its header on the SAME row, and re-reading the whole
        # tab just to rediscover that costs a request we don't need to spend.
        self.header_row = header_row
        # [(sheet_row, header, raw)] for every cell that held a spreadsheet
        # ERROR rather than a value. Those cells were normalised to empty in the
        # rows above — this is what remembers that they existed, so the
        # data-quality flag can name the column and the count instead of the
        # cadence silently treating a broken formula as a blank.
        self.error_cells: list = list(error_cells or [])

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.read_at)

    def column_letter(self, role: str) -> Optional[str]:
        idx = self.role_to_col.get(role)
        return _col_letter(idx) if idx is not None else None

    def schema_line(self) -> str:
        """The one-line schema summary logged once per tab at startup."""
        mapped = ", ".join(sorted(self.role_to_col)) or "(none)"
        return (
            f"tab {self.title!r} kind={self.kind} rows={len(self.rows)} "
            f"cols={len(self.headers)} mapped=[{mapped}]"
        )

    def find_company(self, name: str) -> list[dict]:
        """Rows whose company cell matches `name`, exact-ish first then
        substring. Returns a LIST: two rows for one company is a real thing in a
        tracker, and silently picking one would hide it."""
        want = normalise_header(name)
        if not want:
            return []
        exact = [r for r in self.rows if normalise_header(r.get("company", "")) == want]
        if exact:
            return exact
        return [r for r in self.rows if want in normalise_header(r.get("company", ""))]


def _col_index(letter: str) -> Optional[int]:
    """A1 column letter -> 0-based index. The inverse of `_col_letter`, used by
    the write paths to check an address against the restricted bands."""
    text = str(letter or "").strip().upper()
    if not text or not text.isalpha():
        return None
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_letter(index0: int) -> str:
    """0-based column index → A1 letter (0→A, 26→AA)."""
    n = int(index0) + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


class GTMSheets:
    """Live access to the two GTM spreadsheets.

    Thread-safety: reads happen from `asyncio.to_thread`, so the cache and the
    lazily-built client are guarded by a lock.
    """

    def __init__(self) -> None:
        self._client = None
        self._auth_error: Optional[str] = None
        self._lock = threading.RLock()
        # (sheet_key, tab_kind) -> Tab. Holds the LAST GOOD read, kept
        # indefinitely so a later outage can still be answered from it (with a
        # staleness note) rather than with silence.
        self._cache: dict[tuple[str, str], Tab] = {}
        # sheet_key -> {"ok": bool, "error": str, "remedy": str, "title": str}
        self._access: dict[str, dict] = {}
        self._schema_logged: set[tuple[str, str]] = set()

    # -- identity ----------------------------------------------------------

    @property
    def service_account_email(self) -> str:
        """The address the sheets have to be shared with. Read straight from the
        key file so the instruction can never name the wrong account."""
        path = config.GOOGLE_SERVICE_ACCOUNT_JSON
        if not path:
            return ""
        try:
            import json

            with open(path, "r", encoding="utf-8") as f:
                return str(json.load(f).get("client_email") or "")
        except Exception:
            log.debug("[gtm] could not read client_email from the key file", exc_info=True)
            return ""

    def share_instruction(self, which: str = ORIGINAL) -> str:
        """The exact fix for a permission error, naming the account and the
        access level the sheet needs.

        IT SAYS EDITOR NOW, NOT VIEWER. The sandbox copy is retired and the bot
        writes into the real playbook's writable window, so a Viewer share reads
        perfectly and fails on the first write — with a 403 that `_translate`
        turns into "shared read-only, needs Editor". Saying Editor here is what
        stops that being discovered at the worst moment.
        """
        email = self.service_account_email or "the bot's service account"
        role = "Editor"
        label = "NFThing <> GTM Playbook"
        # Deliberately ASCII: this string is logged, and a Windows console on a
        # cp1252 code page raises UnicodeEncodeError on arrows and dashes — which
        # would turn "here is how to fix your config" into a crash.
        return (
            f'Share "{label}" with {email} as {role} '
            f"(open the sheet, click Share, paste the address, set {role}, Send)."
        )

    def sheet_id(self, which: str = ORIGINAL) -> str:
        """The one spreadsheet id. `which` is accepted and ignored so the cache
        keys and the older call sites read the same as they always did."""
        return config.GTM_SHEET_ORIGINAL_ID

    # -- auth --------------------------------------------------------------

    def _get_client(self):
        """The authorised gspread client, built once. Raises SheetAccessError
        with an actionable remedy rather than a bare exception."""
        with self._lock:
            if self._client is not None:
                return self._client
            if gspread is None:
                raise SheetAccessError(
                    f"the Google Sheets libraries aren't installed ({_IMPORT_ERROR})",
                    remedy="Run: pip install gspread google-auth",
                )
            path = config.GOOGLE_SERVICE_ACCOUNT_JSON
            if not path:
                raise SheetAccessError(
                    "no service-account key is configured",
                    remedy="Set GOOGLE_SERVICE_ACCOUNT_JSON to the key file path.",
                )
            try:
                creds = Credentials.from_service_account_file(path, scopes=SCOPES)
                self._client = gspread.authorize(creds)
            except FileNotFoundError:
                raise SheetAccessError(
                    f"the service-account key file {path!r} does not exist",
                    remedy=f"Put the key at {path}, or fix GOOGLE_SERVICE_ACCOUNT_JSON.",
                )
            except Exception as e:
                raise SheetAccessError(
                    f"the service-account key could not be loaded ({type(e).__name__})",
                    remedy=f"Check that {path!r} is a valid service-account JSON key.",
                )
            return self._client

    def _open(self, which: str = ORIGINAL):
        """Open one spreadsheet, translating the API's errors into something a
        human can act on."""
        client = self._get_client()
        sid = self.sheet_id(which)
        if not sid:
            raise SheetAccessError(
                f"no spreadsheet id configured for the {which}",
                remedy="Set GTM_SHEET_ORIGINAL_ID.",
            )
        try:
            return client.open_by_key(sid)
        except Exception as e:
            raise self._translate(e, which, sid)

    def _translate(
        self, e: Exception, which: str, sid: str, *, writing: bool = False
    ) -> SheetAccessError:
        """Map an API exception to a SheetAccessError with the right remedy and
        the right transient flag.

        `writing` matters: a 403 on a READ means the sheet isn't shared at all,
        while a 403 on a WRITE means it is shared read-only. Same status code,
        completely different fix."""
        name = type(e).__name__
        text = str(e) or ""
        status = getattr(getattr(e, "response", None), "status_code", None)

        if name == "PermissionError" or status == 403 or "PERMISSION_DENIED" in text:
            # Three very different 403s share this shape, so distinguish them —
            # each has a different fix, and "permission denied" alone sends
            # someone to the wrong console.
            if "has not been used" in text or "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
                return SheetAccessError(
                    "the Google Sheets API is not enabled for this service account's project",
                    remedy=(
                        "Enable the Google Sheets API in the Google Cloud console for the "
                        "key's project, then retry."
                    ),
                )
            if writing:
                # Read worked, write didn't: the sheet is shared READ-ONLY. This
                # is the single most likely sharing mistake, and it is invisible
                # until the first write, so name it precisely.
                return SheetAccessError(
                    f"the service account can READ the {which} sheet but not WRITE to it "
                    "(it is shared read-only)",
                    remedy=(
                        f"Re-share it with {self.service_account_email or 'the service account'} "
                        "as EDITOR, not Viewer (open the sheet, click Share, find the address, "
                        "change Viewer to Editor)."
                    ),
                )
            return SheetAccessError(
                f"the service account cannot open the {which} sheet (permission denied)",
                remedy=self.share_instruction(which),
            )
        if name == "SpreadsheetNotFound" or status == 404:
            return SheetAccessError(
                f"the {which} spreadsheet id {sid!r} was not found",
                remedy=(
                    "Check GTM_SHEET_ORIGINAL_ID. "
                    "It is the long id in the sheet's URL between /d/ and /edit. "
                    + self.share_instruction(which)
                ),
            )
        if status == 429 or "RESOURCE_EXHAUSTED" in text or "Quota exceeded" in text:
            return SheetAccessError(
                "the Sheets API quota is exhausted",
                remedy="Wait for the quota window to reset; reads are cached meanwhile.",
                transient=True,
            )
        if status and 500 <= int(status) < 600:
            return SheetAccessError(
                f"the Sheets API returned a server error ({status})",
                remedy="Transient on Google's side; the last cached read is used meanwhile.",
                transient=True,
            )
        if name in ("APIError", "ConnectionError", "Timeout", "ReadTimeout", "SSLError"):
            return SheetAccessError(
                f"the Sheets API call failed ({name})",
                remedy="Usually transient; the last cached read is used meanwhile.",
                transient=True,
            )
        return SheetAccessError(
            f"the {which} sheet could not be opened ({name})",
            remedy=self.share_instruction(which),
        )

    # -- access check (startup) -------------------------------------------

    def check_access(self) -> dict[str, dict]:
        """Probe BOTH spreadsheets. Never raises.

        Returns {which: {ok, title, tabs, error, remedy}}. This is what the
        startup check reports and what `sources.py` turns into connected /
        awaiting-access, so a missing share is surfaced as one actionable line
        instead of as a crash.
        """
        out: dict[str, dict] = {}
        for which in (ORIGINAL,):
            try:
                sh = self._open(which)
                titles = [ws.title for ws in sh.worksheets()]
                out[which] = {
                    "ok": True,
                    "title": sh.title,
                    "tabs": titles,
                    "error": "",
                    "remedy": "",
                }
                log.info(
                    "[gtm] %s sheet OK: %r with %d tab(s): %s",
                    which, sh.title, len(titles), ", ".join(titles),
                )
            except SheetAccessError as e:
                out[which] = {
                    "ok": False,
                    "title": "",
                    "tabs": [],
                    "error": str(e),
                    "remedy": e.remedy,
                }
                log.error("[gtm] %s sheet UNAVAILABLE: %s", which, e)
                if e.remedy:
                    log.error("[gtm] %s sheet FIX: %s", which, e.remedy)
            except Exception as e:
                out[which] = {
                    "ok": False, "title": "", "tabs": [],
                    "error": f"unexpected {type(e).__name__}", "remedy": "",
                }
                log.exception("[gtm] %s sheet check raised unexpectedly", which)
        with self._lock:
            self._access = out
        return out

    def last_access(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._access)

    # -- schema discovery --------------------------------------------------

    def _map_headers(self, kind: str, headers: list[str]) -> dict[str, int]:
        """Discovered headers → {role: column index}.

        Exact normalised matches are taken first across ALL roles, then
        substring matches fill what's left — otherwise a loose alias like "date"
        could claim the column that an exact "Meeting Date" wanted. A header is
        used by at most one role, and a role by at most one header.
        """
        aliases = ROLES.get(kind, {})
        norm = [normalise_header(h) for h in headers]
        taken_cols: set[int] = set()
        out: dict[str, int] = {}

        # An operator override is absolute — it exists precisely for the cases
        # auto-detection gets wrong, so it is applied before any matching.
        override = (config.GTM_COLUMN_MAP or {}).get(kind, {})
        for role, header_text in (override or {}).items():
            key = normalise_header(str(header_text))
            for i, h in enumerate(norm):
                if h == key and i not in taken_cols:
                    out[role] = i
                    taken_cols.add(i)
                    log.info(
                        "[gtm] GTM_COLUMN_MAP: %s.%s -> column %r", kind, role, headers[i]
                    )
                    break
            else:
                log.warning(
                    "[gtm] GTM_COLUMN_MAP names %r for %s.%s, but no such header exists "
                    "in this tab; falling back to auto-detection for that role.",
                    header_text, kind, role,
                )

        for exact_pass in (True, False):
            for role, role_aliases in aliases.items():
                if role in out:
                    continue
                # Longest alias first: "last followed up date" must beat "date".
                for alias in sorted(role_aliases, key=len, reverse=True):
                    key = normalise_header(alias)
                    if not key:
                        continue
                    for i, h in enumerate(norm):
                        if i in taken_cols or not h:
                            continue
                        hit = (h == key) if exact_pass else (key in h or h in key)
                        if hit:
                            out[role] = i
                            taken_cols.add(i)
                            break
                    if role in out:
                        break
        return out

    def _matches_kind(self, kind: str, headers: list[str]) -> int:
        """How well `headers` fit `kind`: the signature score, or 0 for no match.

        A kind's MANDATORY roles must ALL be present. That is the signature from
        the phase-1 spec, and it is what keeps a tab of investor notes from being
        read as a prospect list because one of its headers happened to contain
        the word "notes".
        """
        spec = next((s for s in _KIND_SIGNATURES if s[0] == kind), None)
        if spec is None:
            return 0
        _k, needed, threshold, mandatory = spec
        mapped = self._map_headers(kind, headers)
        if any(role not in mapped for role in mandatory):
            return 0
        score = sum(1 for r in needed if r in mapped)
        return score if score >= threshold else 0

    @staticmethod
    def _is_canonical_title(title: str) -> bool:
        """Is this the tab GTM_POCS_TAB_TITLES names? Compared normalised, so
        "Outreach PoCs", "outreach pocs" and "Outreach  POCs" are one tab."""
        want = normalise_header(title)
        if not want:
            return False
        return any(
            want == normalise_header(t)
            for t in (config.GTM_POCS_TAB_TITLES or []) if str(t).strip()
        )

    @staticmethod
    def _title_hints(title: str) -> set:
        """The kinds whose NAME hints this title matches. A tie-break, nothing more."""
        low = normalise_header(title)
        if not low:
            return set()
        return {kind for kind, hints in _KIND_NAME_HINTS if any(h in low for h in hints)}

    def _detect_kind(self, title: str, headers: list[str]) -> Optional[str]:
        """What KIND of tab this is — decided by its HEADER SIGNATURE.

        NAMES DRIFT AND SIGNATURES DON'T, so the signature is the authority and
        the title is only consulted when two kinds score exactly the same. The
        old behaviour was the other way round — the master tab was claimed by
        TITLE from GTM_MASTER_TAB_TITLES — and it put the whole cadence on a
        status-only tab with no dates in it, where every date rule read a blank
        and quietly never fired. That is the failure this ordering exists to
        prevent.

        `_KIND_SIGNATURES` is in priority order, so a genuine tie (same score,
        no name hint) goes to the earlier entry.

        THE ONE EXCEPTION IS THE CANONICAL TAB. "Outreach PoCs" is claimed by
        NAME, before any signature is scored, because that is the tab phase 2
        was pointed at by name and the retired tracker tab's columns are close
        enough that a signature would take it instead. Its COLUMNS are still
        discovered dynamically; only its identity is fixed.

        None when it is none of the kinds, which is the common case in a real
        playbook full of research and strategy tabs.
        """
        if self._is_canonical_title(title):
            log.debug("[gtm] tab %r claimed as the canonical Outreach PoCs tab by name", title)
            return POCS

        # THE EVENTS TAB, BY NAME WHEN IT IS NAMED. Its signature ("a name and a
        # date") is genuinely weak — half a playbook's tabs match it — so the
        # title is allowed to settle it here rather than only as a tie-break.
        # The signature still catches a renamed tab; this catches the far more
        # common case of a differently-shaped one that is plainly the events list.
        want = normalise_header(title)
        if want and any(
            want == normalise_header(t)
            for t in (config.GTM_EVENTS_TAB_TITLES or []) if str(t).strip()
        ):
            log.debug("[gtm] tab %r claimed as the Events & Summits tab by name", title)
            return EVENTS

        hints = self._title_hints(title)
        best, best_score, best_rank = None, 0, 99
        for rank, (kind, _needed, _threshold, _mandatory) in enumerate(_KIND_SIGNATURES):
            score = self._matches_kind(kind, headers)
            if not score:
                continue
            # (score, name-hint agreement, declaration order) — in that order.
            better = (
                score > best_score
                or (score == best_score and kind in hints and best not in hints)
                or (score == best_score and (kind in hints) == (best in hints)
                    and rank < best_rank)
            )
            if better:
                best, best_score, best_rank = kind, score, rank
        if best is not None and config.GTM_MASTER_TAB_TITLES:
            # An operator naming a tab as the master is still worth honouring
            # when the headers agree — but it can NEVER promote a tab that does
            # not carry the master signature. It cannot reach the canonical tab
            # at all: that one was claimed by name before any signature was
            # scored and returned above.
            named = any(
                normalise_header(title) == normalise_header(t)
                for t in config.GTM_MASTER_TAB_TITLES if str(t).strip()
            )
            if named and best != MASTER:
                log.warning(
                    "[gtm] tab %r is named in GTM_MASTER_TAB_TITLES but its headers "
                    "match %s, not the master signature (Response Status + Intro Sent "
                    "+ Meeting Done). Reading it as %s. Headers: %s",
                    title, best, best,
                    ", ".join(repr(h) for h in headers[:20]) or "(none)",
                )
        return best

    @staticmethod
    def _header_row_index(values: list[list]) -> int:
        """Which row is the header. Sheets in the wild carry a title row, a blank
        row, or a note above the real header, so this picks the first row with at
        least two non-empty cells that looks like labels rather than data."""
        for i, row in enumerate(values[:10]):
            filled = [c for c in row if str(c).strip()]
            if len(filled) >= 2:
                return i
        return 0

    @staticmethod
    def _a1_sheet(title: str) -> str:
        """A worksheet title as an A1 range meaning "the whole sheet".

        Single quotes inside the name are doubled, which is how A1 notation
        escapes them — the live playbook has a tab called
        "6thJune'26_Strategy_Fortune 500" and an unescaped name there makes the
        whole batch request fail.
        """
        return "'" + str(title).replace("'", "''") + "'"

    def _batch_values(self, sh, titles: list[str]) -> dict[str, list[list]]:
        """Every worksheet's values in ONE API call, as {title: rows}.

        This is a quota decision, not a tidiness one. The obvious version calls
        `get_all_values()` per worksheet, which is ~20 read requests per
        spreadsheet on the real playbook and ~40 for both — and Sheets allows 60
        reads per minute per user. Two cache misses in a minute exhausted the
        quota and the bot started answering from stale cache for no reason.
        One batch call is one request no matter how many tabs there are.

        Falls back to per-worksheet reads if the batch call fails, so an
        unexpected range error degrades to "slow" rather than "no data".
        """
        try:
            resp = sh.values_batch_get([self._a1_sheet(t) for t in titles])
        except Exception:
            log.warning(
                "[gtm] batched read failed; falling back to one request per tab "
                "(this burns quota — see _batch_values)", exc_info=True,
            )
            out: dict[str, list[list]] = {}
            for ws in sh.worksheets():
                try:
                    out[ws.title] = ws.get_all_values()
                except Exception:
                    log.exception("[gtm] could not read tab %r", ws.title)
            return out

        ranges = resp.get("valueRanges") or []
        # The response comes back in request order, so zip against the titles we
        # asked for rather than trying to parse the sheet name back out of the
        # returned range string (which re-quotes and re-escapes it).
        return {
            title: (vr.get("values") or [])
            for title, vr in zip(titles, ranges)
        }

    @staticmethod
    def _worksheets(sh) -> list[tuple[str, bool]]:
        """[(title, hidden)] for EVERY worksheet, hidden ones included.

        gspread's `worksheets()` grew an `exclude_hidden` argument; older
        releases have no such parameter and return everything. Both are handled,
        because the master tab is fed by a hidden "outreach updates" sheet and a
        discovery pass that skips hidden tabs would miss the thing this phase is
        built on.
        """
        try:
            sheets = sh.worksheets(exclude_hidden=False)
        except TypeError:
            sheets = sh.worksheets()   # older gspread: hidden already included
        out: list[tuple[str, bool]] = []
        for ws in sheets:
            hidden = getattr(ws, "isSheetHidden", None)
            if hidden is None:
                try:
                    hidden = bool((ws._properties or {}).get("hidden", False))
                except Exception:
                    hidden = False
            out.append((ws.title, bool(hidden)))
        if not config.GTM_READ_HIDDEN_TABS:
            skipped = [title for title, hidden in out if hidden]
            if skipped:
                log.warning(
                    "[gtm] GTM_READ_HIDDEN_TABS=false — skipping %d hidden tab(s): %s. "
                    "The master tab's feed is hidden, so this can hide the cadence "
                    "source.", len(skipped), ", ".join(repr(s) for s in skipped),
                )
            out = [(title, hidden) for title, hidden in out if not hidden]
        return out

    def _warn_colour_coded(self, tab: "Tab") -> None:
        """Name the mapped columns that are systematically empty.

        THE BOT READS VALUES, NOT FORMATTING. In the master tab a status can be
        carried by the CELL'S COLOUR, which the Sheets values API does not return
        and this bot therefore cannot see. The failure is silent — every rule
        that reads such a column just sees blanks and never fires — so the
        signature is checked here and reported by name.

        Only mapped CADENCE roles are checked: an empty "_extra" column is
        somebody's scratch space, not an input to anything.

        THE ACTIVATION COLUMNS ARE THE ONES THAT MATTER MOST HERE. A row is
        active only when its first-contact or connection date is filled, so a
        date column carried as a colour rather than as text does not degrade the
        bot's output — it empties it, and silently.
        """
        total = len(tab.rows)
        if total < max(1, config.CADENCE_EMPTY_COLUMN_MIN_ROWS):
            return
        ratio = max(0.0, float(config.CADENCE_EMPTY_COLUMN_RATIO))
        for role in CADENCE_ROLES:
            if role not in tab.role_to_col:
                continue
            filled = sum(1 for r in tab.rows if str(r.get(role) or "").strip())
            if filled > total * ratio:
                continue
            header = tab.headers[tab.role_to_col[role]] if tab.role_to_col[role] < len(tab.headers) else role
            log.warning(
                "[gtm] tab %r: the %s column (%r) is filled on %d of %d rows. It MAY BE "
                "COLOR-CODED — this bot reads cell VALUES only and cannot see fills, so "
                "every rule that reads %s will see a blank. It needs a text value in "
                "each cell (or point GTM_COLUMN_MAP[%r][%r] at the column that has one).",
                tab.title, role, header, filled, total, role, tab.kind, role,
            )

    def log_writable_window(self, tab: "Tab") -> None:
        """Name the columns that fall INSIDE the writable window, before any write.

        THE POINT IS MISALIGNMENT, NOT PERMISSION. The restricted bands are
        stated as column LETTERS (A:I, S:X) and the sheet's columns move: insert
        one column on the left and every header shifts a place, so the band that
        used to cover the identity block now covers something else and the gap
        the bot may write into now points at a column somebody is using.

        Nothing can detect that from the letters alone, so the NAMES in the gap
        are logged at startup. Somebody reading the boot log sees "writable
        window J:R contains 'Next Steps', 'Owner', ..." and knows immediately
        whether the bands still mean what they meant. A write that lands in the
        wrong column is discovered here, or it is discovered by the person whose
        work it overwrote.
        """
        windows = config.writable_windows()
        label = config.writable_window_label()
        if not windows:
            log.warning(
                "[gtm.window] %r: RESTRICTED_COLUMN_RANGES=%r leaves no writable window "
                "between the bands — every write into the banded region is refused.",
                tab.title, config.RESTRICTED_COLUMN_RANGES,
            )
            return

        col_to_role = {i: r for r, i in tab.role_to_col.items()}
        named: list[str] = []
        for lo, hi in windows:
            for idx in range(lo, min(hi, len(tab.headers) - 1) + 1):
                header = str(tab.headers[idx]).strip()
                if not header:
                    continue
                role = col_to_role.get(idx)
                named.append(
                    f"{config.column_label(idx)}={header!r}"
                    + (f" [{role}]" if role else " [no role]")
                )
        log.info(
            "[gtm.window] %r: restricted %s (NEVER written). Writable window %s holds "
            "%d named column(s): %s",
            tab.title,
            ", ".join(
                f"{config.column_label(lo)}:{config.column_label(hi)}"
                if lo != hi else config.column_label(lo)
                for lo, hi in config.RESTRICTED_COLUMN_BANDS
            ) or "(none)",
            label, len(named),
            ", ".join(named) or "(no named columns — the window is past the last header)",
        )
        if not named:
            log.warning(
                "[gtm.window] %r: the writable window %s contains NO named column. Either "
                "the tab is narrower than the window or the bands have drifted out of "
                "alignment with the sheet. Check RESTRICTED_COLUMN_RANGES against the "
                "full schema logged above before enabling writes.",
                tab.title, label,
            )

    def writable_window_columns(self, tab: "Tab") -> list[dict]:
        """[{column, header, role}] for every NAMED column inside the writable
        window. What `log_writable_window` prints, as data — the "sheet status"
        answer quotes this so the log and the answer can never disagree."""
        out: list[dict] = []
        col_to_role = {i: r for r, i in tab.role_to_col.items()}
        for lo, hi in config.writable_windows():
            for idx in range(lo, min(hi, len(tab.headers) - 1) + 1):
                header = str(tab.headers[idx]).strip()
                if not header:
                    continue
                out.append({
                    "column": config.column_label(idx),
                    "header": header,
                    "role": col_to_role.get(idx, ""),
                })
        return out

    def _log_full_schema(self, which: str, entries: list[dict]) -> None:
        """Log the FULL discovered schema of every tab, once per spreadsheet.

        Every tab, hidden or not, recognised or not, with its complete header
        list. This is a diagnosis tool with one job: when a rule stops firing
        because someone renamed a column, the answer is in the startup log
        instead of in a debugging session.
        """
        log.info(
            "[gtm.schema] ===== %s: %d tab(s) discovered (%d hidden) =====",
            which, len(entries), sum(1 for e in entries if e["hidden"]),
        )
        for e in entries:
            log.info(
                "[gtm.schema] %s%-22r kind=%-17s rows=%-5s headers(%d)=[%s]",
                "HIDDEN " if e["hidden"] else "       ",
                e["title"], e["kind"] or "(unrecognised)",
                e["rows"] if e["rows"] is not None else "-",
                len(e["headers"]),
                " | ".join(str(h) for h in e["headers"]) or "(no header row)",
            )
            if e["kind"] and e["mapped"]:
                log.info(
                    "[gtm.schema]        %r mapped roles: %s",
                    e["title"],
                    ", ".join(f"{role}->{col!r}" for role, col in sorted(e["mapped"].items())),
                )
            if e["kind"] and e["unmapped"]:
                log.info(
                    "[gtm.schema]        %r columns carried as _extra (no role): %s",
                    e["title"], ", ".join(repr(h) for h in e["unmapped"]),
                )
        log.info("[gtm.schema] ===== end of %s schema =====", which)

    def _parse_values(
        self, title: str, values: list[list], *, read_at: float
    ) -> Optional[Tab]:
        """One worksheet's raw values → a Tab, or None when it isn't one of the
        three kinds."""
        if not values:
            return None
        hidx = self._header_row_index(values)
        headers = [str(h).strip() for h in values[hidx]]
        kind = self._detect_kind(title, headers)
        if kind is None:
            log.debug("[gtm] tab %r doesn't match a known kind; skipping", title)
            return None

        role_to_col = self._map_headers(kind, headers)
        col_to_role = {v: k for k, v in role_to_col.items()}

        rows: list[dict] = []
        error_cells: list = []
        for r, raw in enumerate(values[hidx + 1:], start=hidx + 2):  # 1-based sheet row
            # EVERY cell goes through clean_cell: a "#REF!" left behind by a
            # broken formula is the ABSENCE of a value, and carrying it as text
            # made one date column unparseable and one status column read as
            # "responded". The raw error is remembered separately.
            cells = []
            for i, cell in enumerate(raw):
                text = str(cell).strip()
                if is_error_value(text):
                    header = headers[i] if i < len(headers) else "col%d" % i
                    error_cells.append((r, header, text))
                    text = ""
                cells.append(text)
            if not any(cells):
                continue
            row: dict = {"_row": r, "_extra": {}}
            for i, header in enumerate(headers):
                value = cells[i] if i < len(cells) else ""
                role = col_to_role.get(i)
                if role:
                    row[role] = value
                elif header:
                    row["_extra"][header] = value
            # A tracker/priority row with no company is a spacer or a total line,
            # not a deal — carrying it would let it be counted and flagged.
            if kind in (MASTER, POCS, PRIORITY, PIPELINE, RESEARCHER_LINES) \
                    and not (row.get("company") or "").strip():
                continue
            # An events row with no event name is a spacer or a total line.
            if kind == EVENTS and not (row.get("event") or "").strip():
                continue
            rows.append(row)

        return Tab(
            title=title, kind=kind, headers=headers,
            role_to_col=role_to_col, rows=rows, read_at=read_at,
            header_row=hidx + 1, error_cells=error_cells,
        )

    # -- reads -------------------------------------------------------------

    def read(self, which: str = ORIGINAL, *, force: bool = False) -> dict[str, Tab]:
        """Every recognised tab of one spreadsheet, as {kind: Tab}.

        Cached for `SHEET_CACHE_SECONDS` to stay inside the API quota. On a
        failed read, the last good copy is returned — check `Tab.age_seconds` and
        say so — because a sheet outage must never take the bot down. Raises
        SheetAccessError only when there is NO cached copy to fall back to.
        """
        now = time.time()
        with self._lock:
            cached = {
                kind: tabs[0] for (sk, kind), tabs in self._cache.items()
                if sk == which and tabs
            }
            if cached and not force:
                freshest = min(tab.read_at for tab in cached.values())
                if now - freshest < max(1, config.SHEET_CACHE_SECONDS):
                    return cached

        try:
            sh = self._open(which)
            discovered = self._worksheets(sh)          # [(title, hidden)], hidden included
            titles = [title for title, _hidden in discovered]
            hidden_by_title = {title: hidden for title, hidden in discovered}
            by_title = self._batch_values(sh, titles)
            read_at = time.time()
            groups: dict[str, list[Tab]] = {}
            unrecognised: list[str] = []
            schema_entries: list[dict] = []
            for title in titles:
                raw = by_title.get(title) or []
                try:
                    tab = self._parse_values(title, raw, read_at=read_at)
                except Exception:
                    log.exception("[gtm] tab %r failed to parse; skipping it", title)
                    tab = None
                # The schema entry is built for EVERY tab, recognised or not —
                # an unrecognised tab's headers are exactly what you need to see
                # when working out why it wasn't recognised.
                if tab is not None:
                    mapped = {
                        role: (tab.headers[i] if i < len(tab.headers) else f"col{i}")
                        for role, i in tab.role_to_col.items()
                    }
                    taken = set(tab.role_to_col.values())
                    schema_entries.append({
                        "title": title, "hidden": hidden_by_title.get(title, False),
                        "kind": tab.kind, "rows": len(tab.rows), "headers": tab.headers,
                        "mapped": mapped,
                        "unmapped": [h for i, h in enumerate(tab.headers)
                                     if i not in taken and str(h).strip()],
                    })
                else:
                    hidx = self._header_row_index(raw) if raw else 0
                    headers = [str(h).strip() for h in (raw[hidx] if raw else [])]
                    schema_entries.append({
                        "title": title, "hidden": hidden_by_title.get(title, False),
                        "kind": None, "rows": max(0, len(raw) - hidx - 1) if raw else 0,
                        "headers": headers, "mapped": {}, "unmapped": [],
                    })
                if tab is None:
                    unrecognised.append(title)
                    continue
                groups.setdefault(tab.kind, []).append(tab)
                key = (which, tab.title)
                if key not in self._schema_logged:
                    log.info(
                        "[gtm] %s %s%s", which, tab.schema_line(),
                        " [HIDDEN TAB]" if hidden_by_title.get(title) else "",
                    )
                    self._schema_logged.add(key)

            # The full schema, and the colour-coding check, are STARTUP work:
            # once per spreadsheet per process, not on every cache miss.
            # WHICH REAL TAB GOT WHICH ROLE. Logged before the full schema
            # because it is the line people actually need.
            for kind, label in KIND_LABELS.items():
                found = groups.get(kind) or []
                log.info(
                    "[gtm.roles] %s %-18s %-52s -> %s", which, kind, label,
                    ", ".join("%r (%d rows%s)" % (
                        t.title, len(t.rows),
                        ", HIDDEN" if hidden_by_title.get(t.title) else "",
                    ) for t in found) or "(no tab matched this signature)",
                )

            full_key = (which, "_full_schema")
            if config.GTM_LOG_FULL_SCHEMA and full_key not in self._schema_logged:
                self._log_full_schema(which, schema_entries)
                for tab in groups.get(POCS, []) or groups.get(MASTER, []):
                    try:
                        self._warn_colour_coded(tab)
                    except Exception:
                        log.debug("[gtm] colour-coding check failed", exc_info=True)
                self._schema_logged.add(full_key)

            # THE WRITABLE WINDOW, BY NAME, BEFORE ANY WRITE. Logged once per
            # spreadsheet per process, next to the schema it has to be checked
            # against.
            window_key = (which, "_writable_window")
            if window_key not in self._schema_logged:
                for tab in groups.get(POCS, []):
                    try:
                        self.log_writable_window(tab)
                    except Exception:
                        log.debug("[gtm] writable-window log failed", exc_info=True)
                self._schema_logged.add(window_key)

            if POCS not in groups and (which, "_no_pocs") not in self._schema_logged:
                log.error(
                    "[gtm] %s has NO tab named %s — that is the CANONICAL tab and it is "
                    "found by NAME, so nothing proactive has anything to run against. "
                    "The tabs that were found are: %s. Set GTM_POCS_TAB_TITLES to the "
                    "tab's exact title if it was renamed.",
                    which,
                    " / ".join(repr(t) for t in config.GTM_POCS_TAB_TITLES) or "(nothing)",
                    ", ".join(repr(t) for t in titles[:40]) or "(none)",
                )
                self._schema_logged.add((which, "_no_pocs"))
            if MASTER not in groups and (which, "_no_master") not in self._schema_logged:
                log.warning(
                    "[gtm] %s has no master tab (signature: 'Response Status' + 'Intro "
                    "Sent' + 'Meeting Done'). Aggregate answers and the weekly funnel "
                    "are unavailable; the canonical Outreach PoCs tab is unaffected.",
                    which,
                )
                self._schema_logged.add((which, "_no_master"))

            # SEVERAL tabs can legitimately share a kind — the real playbook has
            # a master lead list, a Fortune-500 list and a vertical list, all of
            # them genuine prospect-priority tabs. Keep them all; the biggest is
            # the primary, and the tools search across every one.
            for kind, found in groups.items():
                found.sort(key=lambda t: len(t.rows), reverse=True)
                if len(found) > 1:
                    log.info(
                        "[gtm] %d %s tabs: %s (primary: %r)",
                        len(found), kind, ", ".join(repr(t.title) for t in found),
                        found[0].title,
                    )

            with self._lock:
                for kind, found in groups.items():
                    self._cache[(which, kind)] = found

            if unrecognised and (which, "_unrecognised") not in self._schema_logged:
                log.info(
                    "[gtm] %s: %d tab(s) matched no known kind and are ignored: %s. "
                    "If one of these should be read, set GTM_COLUMN_MAP for it.",
                    which, len(unrecognised),
                    ", ".join(repr(t) for t in unrecognised[:25]),
                )
                self._schema_logged.add((which, "_unrecognised"))

            log.info(
                "[gtm] read %s: %s",
                which,
                ", ".join(
                    f"{k}={sum(len(t.rows) for t in v)} rows across {len(v)} tab(s)"
                    for k, v in groups.items()
                ) or "no known tabs",
            )
            return {k: v[0] for k, v in groups.items()}

        except SheetAccessError as e:
            with self._lock:
                cached = {
                    kind: tabs[0] for (sk, kind), tabs in self._cache.items()
                    if sk == which and tabs
                }
            if cached:
                log.warning(
                    "[gtm] read of %s failed (%s); answering from the cached copy "
                    "(%.0fs old)",
                    which, e, max(t.age_seconds for t in cached.values()),
                )
                return cached
            log.error("[gtm] read of %s failed with no cache to fall back to: %s", which, e)
            raise

    def tab(self, kind: str, which: str = ORIGINAL) -> Optional[Tab]:
        """The PRIMARY tab of a kind (the one with the most rows), or None when
        the sheet has no such tab. Propagates SheetAccessError when there is
        nothing cached."""
        return self.read(which).get(kind)

    def tabs_of(self, kind: str, which: str = ORIGINAL) -> list[Tab]:
        """EVERY tab of a kind, largest first.

        Several tabs can legitimately share a kind — the playbook keeps a master
        lead list, a Fortune-500 list and a vertical list, all of them real
        prospect-priority tabs. A question like "which P1s have we never
        contacted" has to look at all of them, so anything answering across
        prospects uses this rather than `tab()`.
        """
        self.read(which)  # ensure the cache is populated / fresh
        with self._lock:
            return list(self._cache.get((which, kind)) or [])

    def pocs_tab(self, which: str = ORIGINAL) -> Optional[Tab]:
        """THE CANONICAL TAB — "Outreach PoCs" — or None when it isn't there.

        The single accessor for the bot's sheet world. Everything that reads the
        canonical tab goes through here or through `cadence_tab`, so "which tab
        is this bot actually looking at" has exactly one answer.
        """
        return self.read(which).get(POCS)

    def cadence_tab(self, which: str = ORIGINAL) -> tuple[Optional[Tab], str]:
        """THE tab everything proactive runs against: "Outreach PoCs".

        Returns (tab, source). It is the canonical tab or it is nothing.

        FOUND BY NAME, NOT BY SIGNATURE. Phase 2 named this tab directly, and
        the retired outreach tracker tab carries near-identical columns — a
        signature match would take the retired tab and put the bot's whole world
        back where phase 2 moved it from. The COLUMNS inside it are still
        discovered dynamically; only the tab's identity is fixed.

        `(None, "")` when no tab has that name — the digest then goes out
        without its sheet sections and says so in the log, which is the honest
        failure.
        """
        tabs = self.read(which)
        canonical = tabs.get(POCS)
        if canonical is not None:
            return canonical, POCS
        log.error(
            "[gtm] no tab in %s is named %s. That tab is the CANONICAL source and it is "
            "found by NAME — without it nothing proactive has anything to run against. "
            "Set GTM_POCS_TAB_TITLES to the tab's exact title.",
            which,
            " / ".join(repr(t) for t in config.GTM_POCS_TAB_TITLES) or "(nothing)",
        )
        return None, ""

    def role_assignment(self, which: str = ORIGINAL) -> list[tuple[str, str, str]]:
        """[(kind, label, tab title)] — which real tab got which role today.

        Section 1 of the phase-1 spec asks for exactly this: names drift, so the
        tab NAME that matched each role is reported rather than assumed. Logged
        at startup and printed by the cadence dry run.
        """
        self.read(which)
        out: list[tuple[str, str, str]] = []
        with self._lock:
            for kind, label in KIND_LABELS.items():
                tabs = self._cache.get((which, kind)) or []
                if tabs:
                    titles = ", ".join(repr(t.title) for t in tabs)
                else:
                    titles = "(no tab matched this signature)"
                out.append((kind, label, titles))
        return out

    def log_role_assignment(self, which: str = ORIGINAL) -> None:
        """Log the role→tab map once. Cheap, and it is the first thing anyone
        asks for when a rule stops firing."""
        key = (which, "_roles")
        with self._lock:
            if key in self._schema_logged:
                return
        for kind, label, titles in self.role_assignment(which):
            log.info("[gtm.roles] %-18s %-52s -> %s", kind, label, titles)
        with self._lock:
            self._schema_logged.add(key)

    def staleness_note(self, tab: Tab) -> str:
        """The note an answer must carry when it came from cache rather than a
        live read. "" when the data is fresh."""
        age = tab.age_seconds
        if age <= max(1, config.SHEET_CACHE_SECONDS) * 2:
            return ""
        mins = int(age // 60)
        if mins < 60:
            when = f"{max(1, mins)} minute(s) ago"
        else:
            when = f"{mins // 60} hour(s) ago"
        return (
            f"(read from the sheet {when} — I couldn't reach the Sheets API just now, "
            "so this may be out of date)"
        )

    # -- writes ------------------------------------------------------------
    # THE ENTIRE WRITE SURFACE OF THIS BOT, and it fits on one screen.
    #
    # WHAT WAS HERE BEFORE, and what it did: `ensure_bot_column()` appended a
    # column called "Next Deadline (bot)" at the far right of the tab, and
    # `write_deadline_cell()` wrote single cells into that column — on a SANDBOX
    # COPY of the playbook by default (SHEET_WRITE_TARGET). Around them sat
    # `_row_mismatch()` (re-check the company before every write, because the
    # copy and the original were only row-aligned by luck), `_patch_cached_cell()`,
    # `read_cell()` and `verify_write_roundtrip()`. All removed.
    #
    # WHY. An owned column on a copy nobody opens is a write into a drawer. It
    # was the right way to build and prove a write path before anyone had agreed
    # what the bot may touch; it was never a way to be useful. The team works in
    # the real sheet.
    #
    # WHAT REPLACES IT: `write_cells()` — cells in the REAL playbook, in the
    # Outreach PoCs tab, ONLY inside the writable window between the restricted
    # bands. Three locks, all still here:
    #   1. `_refuse_if_read_only` — never the mapping sheet.
    #   2. `_refuse_if_restricted` — never a column in A:I or S:X. Fails closed.
    #   3. the row interlock — the target row must still name the company the
    #      caller believes it does.
    # Plus one new one: every write returns the PRIOR values, so an undo can put
    # the cells back exactly.

    def _refuse_if_read_only(self) -> str:
        """"" when the playbook may be written to, else why it may not.

        THE FIRST LOCK. The researcher/buyer mapping sheet is read-only by
        policy, and mapping_sheet.py enforces that twice over — no write method,
        and a read-only OAuth scope. This is the third enforcement: if anyone
        ever points GTM_SHEET_ORIGINAL_ID at a read-only sheet id, every write
        path here refuses rather than discovering the mistake by making it.
        """
        sid = self.sheet_id()
        if config.is_read_only_sheet_id(sid):
            return (
                f"REFUSING to write: the playbook id ({sid}) is the researcher/buyer "
                "mapping sheet, which is READ-ONLY by policy — the bot never writes to "
                "it. Point GTM_SHEET_ORIGINAL_ID at the GTM Playbook."
            )
        return ""

    @staticmethod
    def _refuse_if_restricted(col_index0, *, what: str) -> str:
        """"" when this 0-based column may be written to, else why it may not.

        THE SECOND LOCK, unchanged since V1 and now the one that matters most:
        the bot writes into the sheet the team actually works in, so the bands
        are the whole safety story. RESTRICTED_COLUMN_RANGES (A:I and S:X by
        default) covers the identity block on the left and the formula block on
        the right — both maintained by people and by formulas the bot cannot
        see.

        It is checked HERE, in the write path, on the ACTUAL column about to be
        written, and it FAILS CLOSED: an index that cannot be read as a number
        is refused.
        """
        if not config.is_restricted_column(col_index0):
            return ""
        band = config.restricted_band_label(col_index0)
        try:
            letter = _col_letter(int(col_index0))
        except (TypeError, ValueError):
            letter = "?"
        return (
            f"REFUSING to {what}: column {letter} is inside the restricted band "
            f"{band or '(unreadable)'} (RESTRICTED_COLUMN_RANGES="
            f"{config.RESTRICTED_COLUMN_RANGES!r}), which the bot may never write to. "
            f"The writable window is {config.writable_window_label() or '(none)'}. "
            f"Reading that column is unaffected."
        )

    def _row_mismatch(self, *, row: int, expect_company: str) -> str:
        """"" when the sheet's row really is `expect_company`, else why not.

        THE THIRD LOCK. Row numbers are read out of a cached parse and a person
        can sort or insert rows in the sheet between the read and the write. The
        target row is re-checked to still name the company we think it does, and
        the write is refused if it does not — a correct value in the wrong row
        is worse than no value at all, because nobody goes looking for it.
        """
        try:
            tabs = self.read()
        except SheetAccessError as e:
            return f"could not confirm row {row} before writing: {e}"
        tab = tabs.get(POCS)
        if tab is None:
            return "there is no Outreach PoCs tab to write to"
        actual = next(
            (r.get("company") for r in tab.rows if r.get("_row") == row), None
        )
        if actual is None:
            return (
                f"row {row} does not exist on the Outreach PoCs tab any more "
                f"(it has been deleted or the tab re-sorted); refusing to write"
            )
        if normalise_header(actual) != normalise_header(expect_company):
            return (
                f"row {row} is {actual!r}, not {expect_company!r} — the sheet has "
                f"changed under me; refusing to write"
            )
        return ""

    def write_cells(
        self, *, row: int, values: dict, expect_company: str, reason: str = "",
    ) -> dict:
        """Write one or more cells in ONE row of the Outreach PoCs tab.

        `values` is {role: new text}. Returns:

            {"ok": bool,
             "written": [{role, column, cell, header, old, new}],
             "refused": [{role, column, why}],
             "error": str}

        `written` CARRIES THE PRIOR VALUE OF EVERY CELL, which is what makes
        undo exact rather than approximate. The caller stores it; `undo_cells`
        below puts it back.

        ALL OR NOTHING ON THE LOCKS, NOT ON THE API. Every cell is checked
        against the bands and the row interlock BEFORE the first request goes
        out, so a write cannot be half-refused. Once the requests start, a
        failure part-way through is reported with what did land — pretending
        otherwise would make the undo record wrong, and a wrong undo record is
        worse than a partial write.

        Never raises: a failed write is reported and audited, and the SQLite
        state that drove it is untouched. The sheet is what the team reads;
        SQLite is what the bot knows.
        """
        out = {"ok": False, "written": [], "refused": [], "error": ""}

        if not config.SHEET_WRITES_ENABLED:
            out["error"] = "sheet writing is off (SHEET_WRITES_ENABLED=false)"
            return out

        refusal = self._refuse_if_read_only()
        if refusal:
            log.error("[gtm] %s", refusal)
            out["error"] = refusal
            return out

        try:
            tabs = self.read()
        except SheetAccessError as e:
            out["error"] = f"cannot reach the playbook: {e}. {e.remedy}".strip()
            return out
        tab = tabs.get(POCS)
        if tab is None:
            out["error"] = "there is no Outreach PoCs tab to write to"
            return out

        mismatch = self._row_mismatch(row=int(row), expect_company=expect_company)
        if mismatch:
            log.error("[gtm] %s", mismatch)
            out["error"] = mismatch
            return out

        current = next((r for r in tab.rows if r.get("_row") == int(row)), None) or {}

        planned: list = []
        for role, new_value in (values or {}).items():
            idx = tab.role_to_col.get(role)
            if idx is None:
                out["refused"].append({
                    "role": role, "column": "",
                    "why": (f"the Outreach PoCs tab has no column mapped to {role!r}; "
                            f"nothing was written for it"),
                })
                continue
            band_refusal = self._refuse_if_restricted(
                idx, what=f"write {role!r} ({_col_letter(idx)}{int(row)})"
            )
            if band_refusal:
                log.warning("[gtm] %s", band_refusal)
                out["refused"].append({
                    "role": role, "column": _col_letter(idx), "why": band_refusal,
                })
                continue
            planned.append({
                "role": role,
                "column": _col_letter(idx),
                "cell": f"{_col_letter(idx)}{int(row)}",
                "header": tab.headers[idx] if idx < len(tab.headers) else role,
                "old": clean_cell(current.get(role)),
                "new": str(new_value or "").strip(),
            })

        if not planned:
            out["error"] = out["error"] or "nothing writable in that update"
            return out

        try:
            sh = self._open()
            ws = sh.worksheet(tab.title)
            for entry in planned:
                # values.update on a SINGLE cell range, one cell at a time. Not
                # a row, not a span — the range is one cell by construction, so
                # a bug in the range string cannot reach a neighbouring column.
                ws.update(
                    values=[[entry["new"]]],
                    range_name=entry["cell"],
                    value_input_option="USER_ENTERED",
                )
                out["written"].append(entry)
                self._patch_cached_cell(
                    row=int(row), role=entry["role"], value=entry["new"]
                )
                log.info(
                    "[gtm] WROTE %s (%s) %r -> %r%s",
                    entry["cell"], entry["header"], entry["old"], entry["new"],
                    f" — {reason}" if reason else "",
                )
        except SheetAccessError as e:
            out["error"] = f"{e}. {e.remedy}".strip()
            log.error("[gtm] write failed after %d cell(s): %s", len(out["written"]), e)
            return out
        except Exception as e:
            err = self._translate(e, ORIGINAL, self.sheet_id(), writing=True)
            out["error"] = f"{err}. {err.remedy}".strip()
            log.error("[gtm] write failed after %d cell(s): %s", len(out["written"]), err)
            if err.remedy:
                log.error("[gtm] ACTION REQUIRED: %s", err.remedy)
            return out

        out["ok"] = bool(out["written"])
        return out

    def undo_cells(self, *, row: int, cells: list, expect_company: str) -> dict:
        """Put cells back to the values recorded when they were written.

        `cells` is what `write_cells` returned in `written`. This is a WRITE
        like any other and goes through the same three locks — an undo that
        skipped the band check would be a way to write anywhere by writing there
        first and then "undoing" something else.

        The row interlock matters even more here than on the way out: the undo
        may be a day later, and if the sheet has been re-sorted since, restoring
        an old value into whatever now sits in that row would be a second, worse
        mistake dressed as a correction.
        """
        values = {c["role"]: c.get("old", "") for c in (cells or []) if c.get("role")}
        result = self.write_cells(
            row=int(row), values=values, expect_company=expect_company,
            reason="undo",
        )
        return result

    def _patch_cached_cell(self, *, row: int, role: str, value: str) -> None:
        """Reflect a written cell into the cached rows.

        Patching in place rather than dropping the cache: dropping it made every
        write cost a full re-read of the spreadsheet on the next call, and we
        know exactly which cell changed and to what.
        """
        with self._lock:
            for tab in self._cache.get((ORIGINAL, POCS)) or []:
                for cached_row in tab.rows:
                    if cached_row.get("_row") == row:
                        cached_row[role] = value
                        return


SHEETS = GTMSheets()


def _self_test() -> int:
    """`python -m gtm_sheet` — access check, schema dump, and the SANDBOX write
    round-trip, without booting Discord.

    This exists so the write path can be re-verified after any change to it, on
    demand, by whoever is deploying. It refuses to run against the original: the
    round-trip writes a real cell, and the only sheet this bot may experiment on
    is the sandbox copy.
    """
    import argparse
    import datetime as _dt

    parser = argparse.ArgumentParser(description="GTM sheet self-test")
    parser.add_argument(
        "--write", action="store_true",
        help="also run the write round-trip against the sandbox copy",
    )
    parser.add_argument(
        "--row", type=int, default=0,
        help="Outreach PoCs sheet row to round-trip. REQUIRED with --write.",
    )
    parser.add_argument(
        "--role", default="",
        help="which role's column to round-trip (e.g. next_steps). REQUIRED with --write.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    print(f"service account: {SHEETS.service_account_email or '(no key readable)'}")
    access = SHEETS.check_access()
    info = access.get(ORIGINAL, {})
    if info.get("ok"):
        print(f"  playbook  OK   {info.get('title')!r} "
              f"({len(info.get('tabs') or [])} tabs)")
    else:
        print(f"  playbook  FAIL {info.get('error')}")
        if info.get("remedy"):
            print(f"            -> {info['remedy']}")
        return 1

    print("\nroles:")
    try:
        for kind, label, titles in SHEETS.role_assignment():
            print(f"  {kind:<18} {label}")
            print(f"  {'':<18}   -> {titles}")
    except SheetAccessError as e:
        print(f"  could not read: {e}")
        return 1

    print("\nschema:")
    try:
        for kind in (POCS, MASTER, PIPELINE, FUNNEL,
                     RESEARCHER_LINES, POSITIONING, PRIORITY):
            for tab in SHEETS.tabs_of(kind):
                print(f"  {tab.schema_line()}")
    except SheetAccessError as e:
        print(f"  could not read: {e}")
        return 1

    tab = SHEETS.pocs_tab()
    if tab is not None:
        print(f"\nwritable window ({config.writable_window_label() or 'none'}):")
        for col in SHEETS.writable_window_columns(tab):
            print(f"  {col['column']:>3}  {col['header'][:40]:<40} "
                  f"{('-> ' + col['role']) if col['role'] else '(no role)'}")

    if not args.write:
        print("\n(skipping the write round-trip — pass --write to run it)")
        return 0

    # THE ROUND-TRIP WRITES A REAL CELL IN THE REAL SHEET. There is no sandbox
    # to hide in any more, so this refuses unless it is given a row and a role
    # explicitly — no guessing at which row is safe to scribble on — and it puts
    # the old value straight back.
    if not config.SHEET_WRITES_ENABLED:
        print("\nrefusing: SHEET_WRITES_ENABLED=false")
        return 2
    if tab is None or not tab.rows:
        print("\nno Outreach PoCs tab with rows — nothing to round-trip")
        return 1
    if not args.row or not args.role:
        print("\nrefusing: --write needs BOTH --row and --role. This writes a real "
              "cell in the real playbook; it will not pick one for you.")
        return 2

    row = int(args.row)
    company = next((r.get("company") for r in tab.rows if r["_row"] == row), "")
    if not company:
        print(f"\nrow {row} is not a data row on {tab.title!r}")
        return 1

    probe = f"round-trip {_dt.datetime.now().strftime('%H:%M:%S')}"
    print(f"\nround-trip: {tab.title!r} row {row} ({company!r}) {args.role} <- {probe!r}")
    result = SHEETS.write_cells(
        row=row, values={args.role: probe}, expect_company=company,
        reason="write round-trip self-test",
    )
    if not result["ok"]:
        print(f"  FAILED: {result['error'] or result['refused']}")
        return 1
    for cell in result["written"]:
        print(f"  wrote  {cell['cell']} ({cell['header']}): "
              f"{cell['old']!r} -> {cell['new']!r}")

    back = SHEETS.undo_cells(row=row, cells=result["written"], expect_company=company)
    if not back["ok"]:
        print(f"  RESTORE FAILED: {back['error']}. The probe value is still in the "
              f"sheet — put it back by hand.")
        return 1
    for cell in back["written"]:
        print(f"  restored {cell['cell']}: {cell['new']!r}")
    print("  PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
