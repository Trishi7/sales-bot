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
from datetime import date, datetime, timezone
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
# "Master Pipeline" is what the live sheet calls the researcher-lines tab, and
# GTM_PIPELINE_TAB_TITLES is what finds it. Same kind, the sheet's own name.
MASTER_PIPELINE = RESEARCHER_LINES
# THE EVENTS & SUMMITS TAB. Conferences, summits and the like, each earning one
# reminder at T-EVENT_LEAD_DAYS and never another.
EVENTS = "events_summits"

# -- THE READ-ONLY CONTEXT TABS ----------------------------------------------
# Three more kinds, all claimed by NAME (see `_claimed_by_name`) and all
# READ-ONLY: no write path addresses them, because every write is planned
# against the canonical tab's writable window and nothing else.
#
# "Deliverables Checklist" — what the team owes, by when. Its deadlines carry no
# year ("25-Sep"), which is why `parse_bare_deadline` exists.
DELIVERABLES = "deliverables_checklist"
# "Sales Packages" — what can actually be sold today, and how finished each
# package is. The bot reads `ready` before it offers one.
PACKAGES = "sales_packages"
# "Q4-OND2026-Goal Setting" — the strategy motions and the goals, as CONTEXT for
# answers. Nothing proactive runs off it and no rule evaluates against it.
GOALS = "goal_setting"

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
    DELIVERABLES: "DELIVERABLES CHECKLIST — what is owed, by when (read-only)",
    PACKAGES: "SALES PACKAGES — what can be sold, and how ready (read-only)",
    GOALS: "GOAL SETTING — the quarter's motions and goals (read-only context)",
    MASTER: "MASTER — status only (aggregates, funnel, cross-check)",
    PIPELINE: "PIPELINE — lead stage / estimated value",
    FUNNEL: "FUNNEL PIVOT — the funnel stage definition",
    RESEARCHER_LINES: "MASTER PIPELINE — researcher outreach lines per company",
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
    "company", "industry", "name", "designation", "based",
    "first_contact", "first_contact_type", "first_contact_date",
    "sid_li_added", "li_connected_date", "li_dm_sent", "li_dm_date",
    "meeting_date", "meeting_status", "next_steps", "package",
    "prospect_status", "closure_prob", "deal_size", "deal_status",
)

# The roles whose presence makes a row ACTIVE. Named here rather than in
# activation.py so that the schema layer and the activation rule cannot drift
# apart: this is the list the startup schema log reports on.
ACTIVATION_ROLES = ("first_contact_date", "li_connected_date")

# The roles the NEXT-ACTION STATE MACHINE reads, on top of the activation ones.
# Named here for the same reason: nextaction.py decides what a row needs next,
# and a header it silently failed to map is a trigger that silently never fires.
# The startup report prints which of these mapped and which did not.
NEXT_ACTION_ROLES = (
    "first_contact", "first_contact_type", "first_contact_date",
    "sid_li_added", "li_connected_date", "li_dm_sent", "li_dm_date",
    "meeting_date", "meeting_status", "next_steps", "package",
    "prospect_status", "closure_prob", "deal_status",
)

# THE TRACKER-ERA ROLES, RETIRED. Every one of these named a column on the tab
# phase 2 left behind, and NONE of them exists on the canonical tab any more.
# They are listed rather than simply deleted so the retirement is LOUD: the
# startup check below names any of them that a live tab still carries, and the
# schema log shows the column as `_extra` instead of silently feeding a rule
# that would then read a blank forever.
#
# The facts some of them carried did not disappear, they were RENAMED — those
# are in `POCS_COMPAT_ALIASES` below, which is a read-side shim, not a role.
RETIRED_POCS_ROLES = (
    "poc_vertical", "phone", "use_case", "intro_date", "last_followed_up",
    "followups_count", "response", "reason", "assets_shared", "other_updates",
    "owner", "status",
)

# RENAMED, NOT RETIRED — the same fact under the column name the sheet now uses.
# Applied to every parsed row and to `Tab.role_to_col` so that code written
# against the old name keeps reading the right cell while it is migrated. It is
# a COMPATIBILITY SHIM WITH A SHELF LIFE, not a second naming scheme: new code
# uses the canonical names on the left-hand side of the sheet's own headers.
#
# It is deliberately one-directional (old -> new). Nothing writes through it:
# `sheetwrite` resolves its target column from the canonical role, so a write
# can never land via an alias nobody meant to keep.
# The header wording each retired role used to answer to, so the startup notice
# can recognise the column and name the role it used to feed.
_RETIRED_ROLE_HEADERS: dict[str, tuple] = {
    "poc_vertical": ("poc vertical", "vertical", "department", "function"),
    "phone": ("phone", "phone number", "mobile", "contact number"),
    "use_case": ("use case", "usecase", "pitch"),
    "intro_date": ("membrane intro date", "intro date", "introduction date"),
    "last_followed_up": ("last followed up date", "last followed up", "last follow up"),
    "followups_count": ("total follow-ups till date", "total follow ups",
                        "total followups", "number of follow-ups"),
    "response": ("response?", "response", "responded", "replied"),
    "reason": ("reason", "lost reason", "reason for no response"),
    "assets_shared": ("assets shared", "assets", "collateral shared"),
    "other_updates": ("other updates", "updates", "comments", "remarks"),
    "owner": ("owner", "row owner", "assigned to", "assignee", "account owner"),
    "status": ("status", "current status"),
}

POCS_COMPAT_ALIASES: dict[str, str] = {
    "poc": "name",
    "poc_designation": "designation",
    "linkedin": "li_url",
    "research_links": "paper_links",
    "first_contacted": "first_contact_date",
    "connected": "li_connected_date",
    "dm_sent_date": "li_dm_date",
    "prospect_stage": "prospect_status",
    "closure": "closure_prob",
    "package_sent": "package",
}

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


def _clock_today():
    """Today, through the bot's clock.

    A yearless date ("23 September") resolves against TODAY, so on a pretend
    day it has to resolve against the pretend today — otherwise a tester on a
    pretend December reads a sheet whose dates were resolved in September.
    Imported lazily: `deadlines` imports this module's siblings and a top-level
    import here would close the loop.
    """
    import clock
    return clock.today_ist()


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
    # THE CANONICAL TAB: "Outreach PoCs", COLUMNS A-X.
    #
    # ITS HEADERS ARE DISCOVERED, NOT ASSUMED — the alias tuples below are how a
    # discovered header is given a role, and GTM_COLUMN_MAP overrides any of
    # them without a code change. What IS fixed is the set of roles: these are
    # the twenty-four facts the tab carries, and the tracker-era roles that used
    # to sit here are retired (see RETIRED_POCS_ROLES).
    #
    # THE FIRST ALIAS OF EACH ROLE IS THE LIVE HEADER, NORMALISED. `_map_headers`
    # runs an EXACT pass across every role before it runs a substring pass, so
    # the live schema maps one-to-one and the looser aliases below it only come
    # into play on a sheet whose wording has drifted. That ordering is what keeps
    # "First Contact", "First Contact Type" and "First Contact Date" — three
    # headers, three facts, one prefix — from collapsing into each other.
    #
    # `first_contact_date` and `li_connected_date` are the two ACTIVATION roles
    # (ACTIVATION_ROLES): a row is invisible to every proactive feature unless
    # one of them holds a date, so a header this list fails to recognise does not
    # degrade the bot's world, it empties it.
    POCS: {
        # A-I: THE IDENTITY BLOCK. Restricted from writing (A:I), never from
        # reading. Mapped so the bot can NAME the column when it refuses to
        # write one — "I never write to Email id" beats "I have no rule for that".
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial", "#"),
        "company": ("company uni", "company/uni", "company", "company name",
                    "university", "uni", "account", "client", "organisation",
                    "organization", "org"),
        "industry": ("industry", "sector", "domain"),
        "name": ("name", "poc name", "contact name", "point of contact",
                 "contact person", "person", "poc"),
        "designation": ("designation", "poc designation", "title", "job title", "role"),
        "email": ("email id", "email", "e mail", "email address", "mail"),
        # WHERE THEY ARE. Read by nothing proactive; quoted constantly in answers
        # ("who do we have in Singapore").
        "based": ("based", "based in", "based at", "location", "city", "country",
                  "geography", "geo", "region"),
        # THE RESEARCH LINKS. The research brief fetches ONLY what is here, and
        # only from RESEARCH_ALLOWED_DOMAINS — it never searches the web and
        # never guesses a URL from a name.
        "paper_links": ("research paper link", "research paper links",
                        "research paper", "paper link", "paper links",
                        "research link", "research links", "papers", "paper",
                        "publications", "publication", "arxiv",
                        "google scholar", "scholar"),
        "li_url": ("li url", "linkedin url", "linkedin", "linkedin profile",
                   "li profile", "profile link", "profile"),

        # J-R: THE WRITABLE WINDOW. The only nine columns any write path can
        # reach, and the reason RESTRICTED_COLUMN_RANGES is A:I,S:X.
        #
        # WHETHER first contact happened at all / who made it. Distinct from its
        # TYPE (K) and its DATE (L): "yes, by Sid, in March" is three facts in
        # three cells, and collapsing them is how a date column ends up holding
        # the word "Yes".
        "first_contact": ("first contact", "first contact by", "first contacted by",
                          "first touch", "contacted"),
        # HOW it was made — email / LinkedIn / call / WhatsApp. Read by the
        # progress check, which drops its "ask for their email address" line
        # when the first contact was already by email. See EMAIL_CONTACT_TYPES.
        "first_contact_type": ("first contact type", "contact type",
                               "type of first contact", "first contact via",
                               "outreach channel", "contacted via", "channel",
                               "medium"),
        # WHEN. An ACTIVATION column.
        "first_contact_date": ("first contact date", "date of first contact",
                               "first contacted date", "first contacted",
                               "outreach date", "date of outreach"),
        # WHETHER THE CONNECTION REQUEST WENT OUT, and from whose account. A
        # request sent is not a request accepted, which is why this is a
        # different cell from the connected DATE beside it.
        "sid_li_added": ("sid li addition", "sid linkedin addition", "sid li added",
                         "li addition", "linkedin addition", "li request sent",
                         "connection request sent"),
        # WHEN THEY ACCEPTED. The other ACTIVATION column.
        "li_connected_date": ("li connected date", "linkedin connected date",
                              "connected date", "connection date", "date connected",
                              "connected on", "li connected"),
        # WHETHER THE DM WENT OUT, and WHEN — again two cells, because
        # connected-and-no-DM and DM-sent-and-no-reply are two different states
        # needing two different next actions, and one date cannot carry both.
        "li_dm_sent": ("li dm sent", "linkedin dm sent", "dm sent", "dm sent?",
                       "message sent"),
        "li_dm_date": ("li dm date", "linkedin dm date", "dm date", "dm sent date",
                       "date dm sent", "message sent date"),
        "meeting_date": ("meeting date", "call date", "demo date", "meeting on",
                         "meeting"),
        # A STATE, NOT A DATE — "booked", "completed", "no-show", "rescheduled".
        # Read it through `normalise_meeting_status` before comparing it.
        "meeting_status": ("meeting status", "meeting state", "meeting outcome",
                           "meeting done", "meeting?"),

        # S-X: THE COMMERCIAL BLOCK. Restricted from writing (S:X) because these
        # are maintained by people and by formulas. Read freely.
        "next_steps": ("next steps notes", "next steps/notes", "next steps",
                       "next step", "next action", "notes", "action"),
        # WHICH PACKAGE went out. Cross-referenced against the Sales Packages
        # tab, which is what says whether that package is finished enough to send.
        "package": ("package", "package sent", "package shared", "pack", "packages"),
        # WHERE THE PROSPECT IS. The quote chase fires off the value "Demo".
        "prospect_status": ("prospect status", "prospect", "prospect stage",
                            "pipeline stage", "funnel stage", "stage"),
        # THE TERMINAL COLUMN. "0%", "Dead", "Unresponsive", "Won", "Lost" stop a
        # row for good; a percentage puts it in the fast or the slow lane. Read
        # it through `closure_percent`, which handles "60%", "0.6" and "60".
        "closure_prob": ("closure prob", "closure prob%", "closure probability",
                         "closure %", "closure", "probability",
                         "close probability", "likelihood of closure"),
        # DEAL SIZE — explicit-command only, never written off a reply. A number
        # somebody mentioned in passing is not a number somebody committed.
        "deal_size": ("estd deal size usd", "estd. deal size (usd)",
                      "estd deal size", "estimated deal size", "deal size",
                      "deal value", "estimated value", "contract value",
                      "ticket size"),
        # In Progress / On Hold. Separate from `closure_prob` because a deal can
        # be 70% and parked, and those need opposite treatment.
        "deal_status": ("deal status", "deal stage", "deal state",
                        "opportunity status", "deal"),
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
    # THE AI EVENTS & SUMMITS TAB, found by name (GTM_EVENTS_TAB_TITLES).
    # Read for its DATES: each event earns ONE reminder at T-EVENT_LEAD_DAYS and
    # never another. Everything else on the row rides into that reminder so it
    # says something useful rather than "there is an event".
    #
    # ITS DATES ARE FREE TEXT and always have been — "15-10-2026" on one row,
    # "October 20-21, 2026" on the next, "not available" on a third. They go
    # through `parse_event_date`, which returns a span and says plainly when it
    # could not read one. A date it cannot parse is UNKNOWN, never today.
    EVENTS: {
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial", "#"),
        "location": ("location", "city", "venue", "where", "geography", "country"),
        "event": ("event name", "event", "summit", "conference", "name"),
        "link": ("link", "url", "website", "site", "registration link", "page"),
        "event_date": ("date", "event date", "dates", "start date", "when"),
        # "9am-5pm", "Day 1: 10:00". Carried into the reminder verbatim; nothing
        # parses it, because nothing needs to.
        "timings": ("timings", "timing", "time", "schedule", "hours"),
        "key_people": ("key people attending", "key people", "people attending",
                       "speakers", "attendees", "who is attending"),
        # A SECOND DEADLINE, and usually the one that actually bites: the event
        # is in November and registration shut in September. Free text like the
        # event date, and read the same way.
        "registration_deadline": ("last day for registration",
                                  "last date for registration",
                                  "registration deadline", "register by",
                                  "registration closes", "rsvp by"),
        # Booleans as people type them — "TRUE", "Yes", "Y", a tick. Read them
        # through `parse_flag`, which returns None for anything it cannot read
        # rather than guessing a No.
        "registered": ("registered?", "registered", "have we registered",
                       "registration done"),
        "attended": ("attended?", "attended", "did we attend", "attendance"),
        # KEPT FOR TABS THAT STILL CARRY THEM. The live tab has none of these;
        # they map to nothing there and cost nothing, and an older events tab
        # that still names them keeps working.
        "event_type": ("type", "event type", "format", "category"),
        "owner": ("owner", "assigned to", "assignee", "responsible", "who"),
        "status": ("status", "attending", "attending?", "decision", "going"),
        "cost": ("cost", "price", "budget", "ticket", "fee"),
        "notes": ("notes", "comments", "remarks", "other updates", "details"),
    },
    # THE DELIVERABLES CHECKLIST, found by name (GTM_DELIVERABLES_TAB_TITLES).
    # What the team owes, to whom it is blocked on, and by when. READ-ONLY.
    #
    # ITS DEADLINES CARRY NO YEAR — "25-Sep", "3-Oct". `parse_bare_deadline`
    # resolves those to the NEXT OCCURRENCE from today, which is the only
    # reading that is right all year: in September "25-Sep" means this month,
    # and in December it means next year. Assuming the current year instead puts
    # every Q1 deadline eleven months in the past and reports the lot as overdue.
    DELIVERABLES: {
        "sr_no": ("sr no", "sr. no.", "s.no", "sno", "serial", "#"),
        "action_item": ("action item", "action", "deliverable", "item", "task",
                        "work item"),
        # WHAT IT IS BLOCKED ON. The reason a deadline slipping is sometimes
        # somebody else's deadline slipping, and worth saying in that order.
        "dependency": ("functional dependency", "dependency", "dependencies",
                       "depends on", "blocked by", "blocker"),
        "priority": ("priority", "priority level", "tier", "band"),
        "deadline": ("tentative deadline", "deadline", "due date", "due",
                     "target date", "eta"),
        "timelines": ("timelines", "timeline", "duration", "effort", "estimate"),
        "link": ("link destination", "link/destination", "destination", "link",
                 "url", "doc", "document"),
        "status": ("status", "state", "progress", "current status"),
        "reminder_freq": ("reminder freq", "reminder frequency", "reminder",
                          "frequency", "cadence", "remind every"),
    },
    # THE SALES PACKAGES TAB, found by name (GTM_PACKAGES_TAB_TITLES).
    # WHAT CAN ACTUALLY BE SOLD TODAY, and how finished each package is.
    # READ-ONLY, and the point of reading it is `ready`: offering a prospect a
    # package that is 40% built is a promise somebody else has to keep.
    #
    # `ready` IS BLANK ON MOST ROWS, and blank means NO (`ready_flag`). That is
    # the safe direction and the only one: a package nobody has marked ready is
    # a package nobody has said is ready.
    PACKAGES: {
        "package": ("package", "package id", "package code", "pkg", "sr no", "#"),
        "name": ("name", "package name", "title"),
        "purpose": ("purpose", "why", "objective", "intent"),
        "use_case": ("use case", "usecase", "case", "application"),
        "size": ("size", "volume", "scale", "quantity"),
        "audio_files": ("audio files", "audio file", "audio", "audios"),
        "image_files": ("image files", "image file", "images", "image"),
        "jsonl_output": ("jsonl output", "jsonl", "output", "json output"),
        "product_doc": ("pulse product doc", "pulse_product doc", "pulse doc",
                        "product doc", "spec doc", "doc"),
        # "% Completion" normalises to "completion" — the per cent sign is
        # punctuation and `normalise_header` drops it.
        "completion": ("completion", "% completion", "completion %",
                       "percent completion", "progress", "percent complete"),
        "ready": ("ready?", "ready", "is ready", "ready to sell", "sellable"),
        "status": ("status", "state", "current status"),
    },
    # THE QUARTER'S GOAL-SETTING TAB, found by name (GTM_GOALS_TAB_TITLES).
    # TWO TABLES STACKED IN ONE SHEET: the strategy motions, then the goals.
    #
    # READ-ONLY CONTEXT FOR ANSWERS, AND NOTHING ELSE. No rule evaluates against
    # it, nothing proactive fires off it, and no write path can reach it. It is
    # there so that "what is this quarter committed to" has an answer the bot
    # can cite instead of infer.
    #
    # THE MAPPING IS DELIBERATELY LOOSE. One header row cannot describe two
    # tables, so `_header_row_index` picks the first and everything the roles
    # below do not claim is carried verbatim in `_extra`, keyed by its own
    # header. A question about the second table is answerable from `_extra` even
    # though no role here names its columns.
    GOALS: {
        "motion": ("strategy motion", "strategy motions", "motion", "strategy",
                   "gtm motion", "play", "lever"),
        "goal": ("goal", "goals", "objective", "outcome", "target outcome"),
        "metric": ("metric", "measure", "how measured", "measurement"),
        "target": ("target", "q4 target", "number", "value", "goal value"),
        "owner": ("owner", "dri", "responsible", "assigned to", "who"),
        "timeline": ("timeline", "timelines", "by when", "when", "quarter",
                     "deadline", "due"),
        "status": ("status", "state", "progress"),
        "notes": ("notes", "comments", "remarks", "details", "context"),
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

# THE NAME-CLAIMED TABS. Each of these is found by its TITLE, before any
# signature is scored, and each has an env var holding its candidate titles.
#
# WHY NAMES HERE AND SIGNATURES EVERYWHERE ELSE. A signature describes a SHAPE,
# and this playbook has several tabs of each shape: "Deliverables Checklist" and
# "Sales Packages" are both a list of things with a status; the events tab and
# the goal-setting tab are both a name and a date. A signature would have to
# choose between them on a scoring margin, and the wrong choice is silent — the
# bot reads the goals tab as the events list and reminds nobody about anything.
# These tabs were named to us directly, so naming them is the honest mechanism.
#
# THE ORDER MATTERS: the first entry whose titles match wins, and the canonical
# tab is first so that nothing can take the kind the whole bot runs on.
#
# Each value is a callable rather than the list itself so that the CURRENT value
# of the setting is read on every call. A module-level snapshot would freeze
# whatever the env said at import time, which is exactly the bug the whole
# "re-point the bot with an env change" design exists to avoid.
_NAME_CLAIMED_KINDS: tuple = (
    (POCS, lambda: config.GTM_POCS_TAB_TITLES),
    (DELIVERABLES, lambda: config.GTM_DELIVERABLES_TAB_TITLES),
    (RESEARCHER_LINES, lambda: config.GTM_PIPELINE_TAB_TITLES),
    (PACKAGES, lambda: config.GTM_PACKAGES_TAB_TITLES),
    (EVENTS, lambda: config.GTM_EVENTS_TAB_TITLES),
    (GOALS, lambda: config.GTM_GOALS_TAB_TITLES),
)

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


# -- VALUE NORMALISATION ------------------------------------------------------
# THE SHEET IS TYPED BY PEOPLE, so one fact arrives in half a dozen spellings.
# "60%", "0.6" and "60" are the same closure probability; "TRUE", "Yes", "Y" and
# a tick are the same yes; "completed", "Completed" and "done" are the same
# meeting.
#
# EVERY ONE OF THESE RETURNS None — or, where a blank has an agreed meaning, the
# SAFE value — rather than guessing when it cannot read the cell. That is the
# whole discipline: a normaliser that guesses turns a typo into a fact, and then
# the bot repeats that fact back to people as though somebody had typed it.

# Ticks and crosses, matched on the RAW cell. `normalise_header` strips
# punctuation, which would eat them, so the symbols are checked before it runs.
_TRUE_MARKS = {"✓", "✔", "☑", "✅"}
_FALSE_MARKS = {"✗", "✘", "✖", "❌", "☐"}

_TRUE_WORDS = {
    "true", "yes", "y", "done", "sent", "completed", "complete", "connected",
    "1", "ok", "okay", "confirmed", "registered", "attended",
}
_FALSE_WORDS = {
    "false", "no", "n", "not yet", "none", "nil", "0", "pending", "na", "n a",
    "not applicable", "not done", "-",
}

# What the sheet says when NOBODY KNOWS YET. Distinct from blank, and the
# distinction earns its keep: "not available" is somebody having looked and
# found nothing, and a date parser that treated it as a parse failure would keep
# reporting a filled-in cell as a schema problem.
_UNKNOWN_WORDS = {
    "not available", "na", "n a", "tbd", "tba", "unknown", "?",
    "to be confirmed", "to be announced", "not announced", "not yet announced",
    "not confirmed",
}


def is_unknown_value(value) -> bool:
    """True when a cell says, in so many words, that nobody knows yet.

    "the sheet says the date is not available" and "the sheet does not say" are
    different sentences, and only the second one is a gap somebody should fill.
    """
    return normalise_header(clean_cell(value)).strip() in _UNKNOWN_WORDS


def parse_flag(value) -> Optional[bool]:
    """A people-typed boolean. True / False / None when it is neither.

    "TRUE", "Yes", "Y" and a tick are True; "FALSE", "No", "N" and a cross are
    False; a blank, a sentence, or anything unrecognised is None.

    NONE IS NOT FALSE, and keeping them apart is the point. "Registered? = No"
    is a decision somebody made; "Registered? = (blank)" is a question nobody
    has answered, and a bot that reports the second as the first is inventing a
    decision. A caller that wants blank to mean no says so itself — `ready_flag`
    is the one place in this file that does.
    """
    raw = clean_cell(value).strip()
    if not raw:
        return None
    if raw in _TRUE_MARKS:
        return True
    if raw in _FALSE_MARKS:
        return False
    word = normalise_header(raw).replace("?", "").strip()
    if not word:
        return None
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return None


def ready_flag(value) -> str:
    """The Sales Packages "Ready?" cell as "Yes" or "No". BLANK IS NO.

    The one place in this module a blank reads as a negative, and deliberately:
    a package nobody has marked ready is a package nobody has said is ready, and
    the two mistakes do not cost the same. Calling a finished package unready
    costs one question. Offering a prospect a half-built one costs a promise
    somebody else then has to keep.
    """
    return "Yes" if parse_flag(value) is True else "No"


def closure_percent(value) -> Optional[int]:
    """A closure-probability cell as a whole percentage. None when it isn't one.

    "60%", "60", "60 %" and "0.6" all read as 60.

    A FRACTION IS RECOGNISED ONLY BELOW 1. In a percentage column "0.6" means
    six tenths and "60" means six tenths, and reading 0.6 as six tenths of ONE
    PER CENT would quietly move a live deal into the slow lane. An explicit 0
    stays 0 — zero is a terminal value here, not a missing one.
    """
    raw = clean_cell(value).replace("%", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 < number < 1:
        number *= 100
    if number < 0 or number > 100:
        return None
    return int(round(number))


# The meeting states this bot reasons about, and the words a sheet writes them
# with. Anything unrecognised comes back normalised but UNCHANGED rather than
# forced into one of these — an unmapped state is a state nobody told the bot
# about, and inventing a mapping for it is how "rescheduled" becomes "done".
_MEETING_STATUS_WORDS = {
    "completed": ("completed", "complete", "done", "held", "happened",
                  "met", "finished", "occurred", "meeting done"),
    "booked": ("booked", "scheduled", "confirmed", "set", "fixed", "planned",
               "upcoming"),
    "no_show": ("no show", "noshow", "did not attend", "didnt attend",
                "no showed"),
    "rescheduled": ("rescheduled", "reschedule", "moved", "postponed", "pushed"),
    "cancelled": ("cancelled", "canceled", "called off", "dropped"),
}


def normalise_meeting_status(value) -> str:
    """A meeting-status cell as one of the states above, or "" when blank.

    "completed", "Completed", "COMPLETED" and "done" all return "completed".
    """
    word = normalise_header(clean_cell(value)).replace("?", "").strip()
    if not word:
        return ""
    for state, spellings in _MEETING_STATUS_WORDS.items():
        if word in spellings:
            return state
    return word


def is_meeting_completed(value) -> bool:
    """True only when the cell says the meeting actually HAPPENED.

    A booked meeting is not a completed one, and the gap between those two is
    the entire follow-up. This is why `meeting_status` is a separate column from
    `meeting_date`: a date says when, and only this says whether.
    """
    return normalise_meeting_status(value) == "completed"


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"

# "25-Sep", "3 Oct", "25/09" — a deadline with no year, which is how the
# Deliverables Checklist is written.
_BARE_DEADLINE_RE = re.compile(
    r"^\s*(\d{1,2})\s*[-/ ]\s*(" + _MONTH_NAMES + r")[a-z]*\.?\s*$",
    re.IGNORECASE,
)


def parse_bare_deadline(value, *, today=None):
    """"25-Sep" -> the NEXT 25 September from today. None when it isn't one.

    THE NEXT OCCURRENCE, NOT THIS YEAR'S. A yearless deadline is written by
    somebody who means "the one coming up", and that is the only reading that is
    right in every month of the year. Assuming the CURRENT year instead puts
    every January deadline eleven months in the past the moment February
    arrives, and reports the whole checklist as overdue.

    TODAY COUNTS AS THE NEXT OCCURRENCE. A deadline of today is due today, not
    due in a year.

    A cell carrying a FULL date ("25-Sep-2026") returns None here on purpose, so
    the caller falls through to `sheet_date` — which knows about years — rather
    than having this function silently re-date it.
    """
    match = _BARE_DEADLINE_RE.match(clean_cell(value))
    if not match:
        return None
    day = int(match.group(1))
    month = _MONTHS.get(match.group(2).lower()[:3])
    if not month:
        return None
    base = today or _clock_today()
    for year in (base.year, base.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            # 29 February in a non-leap year. Try the next year rather than
            # giving up: the deadline is real, this calendar just has no such day.
            continue
        if candidate >= base:
            return candidate
    return None


# FREE-TEXT EVENT DATES, IN THE FOUR SHAPES THE LIVE TAB ACTUALLY USES. Every
# one of these is in the Date column right now, which is why there are four
# patterns and not one:
#   "15-10-2026", "15/10/2026"              numeric, day-first
#   "October 20-21,2026"                    month first, a range, a year
#   "4-5 November 2026", "13-15 Oct, 2026"  DAY FIRST, a range, a year
#   "23 September"                          day first, NO YEAR
#
# Built with chr() rather than an escape so the class is unambiguous in the
# source: these are hyphen, EN DASH and EM DASH, and a reader should not have to
# work out which of the three a font is showing them.
_DASH_CLASS = "[-" + chr(0x2013) + chr(0x2014) + "]"

_EVENT_DMY_RE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b")

# "October 20-21, 2026" / "Oct 5, 2026". The year is OPTIONAL: "October 20-21"
# on its own resolves to the next occurrence, like every other yearless date in
# this module.
_EVENT_MONTH_RE = re.compile(
    r"\b(" + _MONTH_NAMES + r")[a-z]*\.?\s+(\d{1,2})"
    r"(?:\s*" + _DASH_CLASS + r"\s*(\d{1,2}))?\s*,?\s*(\d{4})?\b",
    re.IGNORECASE,
)

# "4-5 November 2026" / "25-26 November 2026" / "13-15 Oct, 2026" / "23 September".
# The mirror image of the pattern above, and the shape the live tab uses most.
_EVENT_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:\s*" + _DASH_CLASS + r"\s*(\d{1,2}))?\s*(?:st|nd|rd|th)?\s+"
    r"(?:of\s+)?(" + _MONTH_NAMES + r")[a-z]*\.?\s*,?\s*(\d{4})?\b",
    re.IGNORECASE,
)


def _next_occurrence(month: int, day: int, today=None):
    """A month and a day with NO YEAR -> the next date that matches.

    The same rule `parse_bare_deadline` uses, for the same reason: a yearless
    date is written by somebody who means the one coming up, and "23 September"
    read as the current year's is in the past for three quarters of the year.
    """
    base = today or _clock_today()
    for year in (base.year, base.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= base:
            return candidate
    return None


def parse_event_date(value) -> dict:
    """A free-text event date as {start, end, text, known, year_assumed, reason}.

    THE EVENTS TAB'S DATE COLUMN IS PROSE AND ALWAYS HAS BEEN. The live tab
    carries all of these at once: "15-10-2026", "October 20-21,2026",
    "4-5 November 2026", "13-15 Oct, 2026", "23 September" and "not available".
    All of them are read except the last, which is an ANSWER rather than a
    failure and is reported as one.

    A RANGE returns its first day as `start` and its last as `end`, because a
    reminder fires off the day the thing begins.

    A DATE WITH NO YEAR resolves to the NEXT OCCURRENCE and sets `year_assumed`,
    so a caller can say "23 September, assuming the next one" instead of stating
    a year nobody wrote.

    `known` is False for anything unreadable, and `reason` says WHICH KIND of
    unreadable it was. "The sheet says it is not available" and "I cannot read
    this date" are different sentences, and only the second is worth anyone's
    time to fix.

    DAY-FIRST, NOT MONTH-FIRST, for the numeric form. "15-10-2026" is 15
    October: this team writes dates the way most of the world does, and the rest
    of this module already assumes it (`sheet_date`). A value that cannot BE
    day-first is reported unreadable rather than silently swapped, because a bot
    that quietly reinterprets one date will quietly reinterpret a real one.
    """
    text = clean_cell(value)
    out = {"start": None, "end": None, "text": text, "known": False,
           "year_assumed": False, "reason": ""}
    if not text:
        out["reason"] = "the cell is empty"
        return out
    if is_unknown_value(text):
        out["reason"] = "the sheet says the date is not available"
        return out

    match = _EVENT_DMY_RE.search(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            out["start"] = out["end"] = date(year, month, day)
            out["known"] = True
        except ValueError:
            out["reason"] = "%r is not a real day-first date" % text
        return out

    # Month-first, then day-first. Both may carry a range and both may omit the
    # year. Month-first is tried first because "October 20-21, 2026" would
    # otherwise let the day-first pattern match "21, 2026" as "21 <no month>".
    for pattern, month_first in ((_EVENT_MONTH_RE, True), (_EVENT_DAY_MONTH_RE, False)):
        match = pattern.search(text)
        if not match:
            continue
        if month_first:
            name, first, last, year = match.groups()
        else:
            first, last, name, year = match.groups()
        month = _MONTHS.get(name.lower()[:3])
        if not month:
            continue
        if year:
            try:
                start = date(int(year), month, int(first))
                end = date(int(year), month, int(last)) if last else start
            except ValueError:
                out["reason"] = "%r names a day that month does not have" % text
                return out
        else:
            start = _next_occurrence(month, int(first))
            end = _next_occurrence(month, int(last)) if last else start
            if start is None:
                out["reason"] = "%r names a day that month does not have" % text
                return out
            out["year_assumed"] = True
        out["start"] = start
        # "October 30-2" is a range running backwards — somebody meant it to
        # cross a month boundary and this column cannot express that. Take the
        # start day rather than inventing a span nobody wrote.
        out["end"] = end if (end and end >= start) else start
        out["known"] = True
        return out

    parsed = sheet_date(text)
    if parsed:
        out["start"] = out["end"] = parsed
        out["known"] = True
        return out

    out["reason"] = "I cannot read %r as a date" % text
    return out


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
                 header_row: int = 1, error_cells: Optional[list] = None,
                 canonical_role_to_col: Optional[dict] = None):
        self.title = title
        self.kind = kind
        self.headers = headers
        self.role_to_col = role_to_col      # role -> 0-based column index
        # THE SAME MAP WITHOUT THE COMPATIBILITY ALIASES. `role_to_col` above
        # carries the renamed-role shims (POCS_COMPAT_ALIASES) so that code not
        # yet migrated still resolves to the right column; this one carries only
        # the roles the SHEET actually has.
        #
        # Anything that INVERTS the map — the schema log, the writable-window
        # report, the "which role is this column" lookups — must use this one.
        # Inverting the aliased map is many-to-one, so the column that is really
        # `name` would report itself as `poc` about half the time, depending on
        # dict ordering. A diagnostic that reports a different answer on
        # different runs is worse than no diagnostic.
        self.canonical_role_to_col = dict(canonical_role_to_col or role_to_col)
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
        mapped = ", ".join(sorted(self.canonical_role_to_col)) or "(none)"
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
        # THE LAST FULL DISCOVERY, per spreadsheet: every tab, recognised or
        # not, with its kind, row count and headers. Kept so "sheet status" can
        # report what the bot found WITHOUT a second round trip to the API, and
        # so the answer it gives is the same discovery the startup log printed
        # rather than a fresh one that might disagree with it.
        self._last_schema: dict[str, list[dict]] = {}

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
    def _claimed_by_name(title: str) -> Optional[str]:
        """The kind this TITLE is named for, or None.

        Compared through `normalise_header`, so "AI Events & Summits",
        "ai events and summits" and "AI  Events  &  Summits" are one tab.

        First match wins and the canonical tab is checked first, so a title
        listed in two env vars by mistake resolves deterministically instead of
        depending on dict ordering. The clash is logged, because two settings
        naming one tab is a configuration error somebody should fix.
        """
        want = normalise_header(title)
        if not want:
            return None
        claimed: list = []
        for kind, titles_of in _NAME_CLAIMED_KINDS:
            for candidate in (titles_of() or []):
                if str(candidate).strip() and want == normalise_header(candidate):
                    claimed.append(kind)
                    break
        if not claimed:
            return None
        if len(claimed) > 1:
            log.warning(
                "[gtm] tab %r is named by more than one setting (%s). Reading it as %s "
                "— the first match wins. Remove it from the others so the two settings "
                "cannot disagree.", title, ", ".join(claimed), claimed[0],
            )
        return claimed[0]

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

        THE EXCEPTIONS ARE THE NAME-CLAIMED TABS (`_NAME_CLAIMED_KINDS`): the
        canonical Outreach PoCs tab, the Deliverables Checklist, the Master
        Pipeline, the Sales Packages tab, the AI Events & Summits tab and the
        goal-setting tab. Each is claimed by TITLE before any signature is
        scored, because each shares its shape with another tab in this playbook
        and a scoring margin is not a safe way to tell them apart. THEIR COLUMNS
        ARE STILL DISCOVERED DYNAMICALLY; only their identity is fixed.

        None when it is none of the kinds, which is the common case in a real
        playbook full of research and strategy tabs.
        """
        named = self._claimed_by_name(title)
        if named is not None:
            log.debug(
                "[gtm] tab %r claimed as %s by name", title, KIND_LABELS.get(named, named),
            )
            return named

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

        col_to_role = {i: r for r, i in tab.canonical_role_to_col.items()}
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
        col_to_role = {i: r for r, i in tab.canonical_role_to_col.items()}
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

        canonical_role_to_col = self._map_headers(kind, headers)
        col_to_role = {v: k for k, v in canonical_role_to_col.items()}

        # THE COMPATIBILITY SHIM, applied once per tab rather than once per
        # lookup. An old role name resolves to the column its renamed successor
        # found, and only when that successor actually mapped — an alias
        # pointing at a column that does not exist would be a worse lie than the
        # missing role it replaced.
        role_to_col = dict(canonical_role_to_col)
        if kind == POCS:
            for old_role, new_role in POCS_COMPAT_ALIASES.items():
                if new_role in canonical_role_to_col and old_role not in role_to_col:
                    role_to_col[old_role] = canonical_role_to_col[new_role]

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
            # THE COMPATIBILITY ALIASES, on the row as well as on the map.
            # Code that reads `row["poc"]` gets the Name cell; code that reads
            # `row["first_contacted"]` gets the First Contact Date cell. Filled
            # from the canonical value, never the other way round, so there is
            # still exactly one cell behind each fact.
            if kind == POCS:
                for old_role, new_role in POCS_COMPAT_ALIASES.items():
                    if new_role in row and old_role not in row:
                        row[old_role] = row[new_role]

            # A SPACER OR A TOTAL LINE IS NOT A ROW. Every kind below has one
            # column that a real row cannot be missing, and a blank in it means
            # the line is formatting rather than data. Carrying those let a
            # "TOTAL" line be counted as a deal and flagged as one.
            if kind in (MASTER, POCS, PRIORITY, PIPELINE, RESEARCHER_LINES) \
                    and not (row.get("company") or "").strip():
                continue
            if kind == EVENTS and not (row.get("event") or "").strip():
                continue
            if kind == DELIVERABLES and not (row.get("action_item") or "").strip():
                continue
            if kind == PACKAGES and not (
                (row.get("name") or "").strip() or (row.get("package") or "").strip()
            ):
                continue
            # THE GOALS TAB IS DELIBERATELY NOT FILTERED. It holds two stacked
            # tables under one header row, so a row that maps no role at all is
            # very likely the second table's content — exactly the thing the
            # answers need. Its columns ride in `_extra`, keyed by their own
            # headers, and a blank-line drop is already handled above.
            rows.append(row)

        if kind == POCS:
            self._warn_retired_roles(title, headers)

        return Tab(
            title=title, kind=kind, headers=headers,
            role_to_col=role_to_col, rows=rows, read_at=read_at,
            header_row=hidx + 1, error_cells=error_cells,
            canonical_role_to_col=canonical_role_to_col,
        )

    @staticmethod
    def _warn_retired_roles(title: str, headers: list[str]) -> None:
        """Name any header on the canonical tab that a RETIRED role used to own.

        THE RETIREMENT HAS TO BE AUDIBLE. The tracker-era roles were removed
        from ROLES[POCS], which means a tab still carrying "Total Follow-ups" or
        "Response?" now files that column under `_extra` and every rule that
        used to read it sees nothing. That is the intended behaviour — those
        rules ran on a tab phase 2 retired — but it is indistinguishable from a
        column rename nobody noticed unless somebody says so out loud.

        Logged once per read of the tab, at INFO: it is not an error, it is the
        bot telling you which columns it has deliberately stopped reading.
        """
        norm = {normalise_header(h) for h in headers if str(h).strip()}
        still_there = sorted(
            role for role in RETIRED_POCS_ROLES
            if any(normalise_header(alias) in norm
                   for alias in _RETIRED_ROLE_HEADERS.get(role, (role,)))
        )
        if still_there:
            log.info(
                "[gtm] tab %r still carries column(s) for %d RETIRED role(s): %s. Those "
                "are tracker-era columns: they are read into `_extra` and quoted in "
                "answers, but no rule evaluates them any more. Nothing to fix unless you "
                "expected one of them to drive a reminder.",
                title, len(still_there), ", ".join(still_there),
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
                        for role, i in tab.canonical_role_to_col.items()
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
                    ) for t in found)
                    or ("(NO TAB CLAIMED THIS NAME — check the *_TAB_TITLES "
                        "setting for this kind against the schema below)"
                        if kind in {k for k, _ in _NAME_CLAIMED_KINDS}
                        else "(no tab matched this signature)"),
                )

            self._last_schema[which] = list(schema_entries)

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

    def discovered_tabs(self, which: str = ORIGINAL) -> list[dict]:
        """EVERY tab in the spreadsheet, recognised or not, as plain dicts.

        [{title, hidden, kind, kind_label, rows, columns, headers, mapped_roles,
          unmapped_headers}], in sheet order.

        WHAT THE STARTUP LOG PRINTS, AS DATA. The "sheet status" answer is built
        from this for the same reason `writable_window_columns` exists: a
        verification answer assembled separately from the log it is verifying
        can disagree with it, and then neither one can be trusted.

        Includes the UNRECOGNISED tabs deliberately — "I can see this tab and I
        do not read it" is the most useful line in the whole report when
        somebody has just renamed something.
        """
        self.read(which)   # ensure discovery has run at least once
        with self._lock:
            entries = list(self._last_schema.get(which) or [])
        out: list[dict] = []
        for e in entries:
            out.append({
                "title": e["title"],
                "hidden": bool(e["hidden"]),
                "kind": e["kind"] or "",
                "kind_label": KIND_LABELS.get(e["kind"] or "", "") if e["kind"]
                else "not read — this tab matches no known kind",
                "rows": e["rows"],
                "columns": len(e["headers"]),
                "headers": list(e["headers"]),
                "mapped_roles": dict(e["mapped"]),
                "unmapped_headers": list(e["unmapped"]),
            })
        return out

    # -- APPENDING A ROW ---------------------------------------------------

    def find_duplicate(self, tab: "Tab", *, company: str, poc: str = "") -> Optional[dict]:
        """The existing row this append would duplicate, or None.

        NORMALISED THE SAME WAY THE NEWS-SCREEN MATCHER IS — `normalise_header`,
        lower-cased with punctuation collapsed. "Wispr Flow", "wispr flow" and
        "Wispr  Flow." are one company, and an append that did not think so
        would put a second row for the same account into a sheet people read as
        one-row-per-account.

        COMPANY ALONE for the pipeline and events tabs; COMPANY PLUS PERSON for
        Outreach PoCs, where several rows per company is the normal shape and
        only the same PERSON at the same company is a duplicate.
        """
        want_co = normalise_header(company)
        if not want_co:
            return None
        want_poc = normalise_header(poc)
        for row in (tab.rows or []):
            have_co = normalise_header(clean_cell(row.get("company")))
            if have_co != want_co:
                continue
            if tab.kind != POCS or not want_poc:
                return row
            have_poc = normalise_header(
                clean_cell(row.get("name") or row.get("poc") or row.get("event"))
            )
            if have_poc == want_poc:
                return row
        return None

    def first_empty_row(self, tab: "Tab", sh=None) -> int:
        """The 1-based sheet row an append may write into.

        THE FIRST ROW AFTER THE LAST ONE HOLDING ANYTHING, and it is computed
        from a FRESH read rather than from `tab.rows`. `tab.rows` has had blank
        and spacer rows filtered out of it — that is what makes it useful
        everywhere else and useless here, because the row numbers it kept are
        not a contiguous range and its last entry is not necessarily the last
        occupied row.

        NEVER A ROW WITH ANY EXISTING VALUE. The caller checks again before
        writing (`append_row`), because this is arithmetic and that is the
        actual guarantee.
        """
        try:
            sh = sh or self._open(ORIGINAL)
            ws = sh.worksheet(tab.title)
            values = ws.get_all_values()
        except Exception:
            log.exception("[gtm] could not read %r to find its last row", tab.title)
            return 0
        last = 0
        for i, row in enumerate(values, start=1):
            if any(str(c).strip() for c in (row or [])):
                last = i
        return last + 1

    def _next_sr_no(self, tab: "Tab") -> str:
        """The next serial number, or "" when the tab has no Sr No column.

        MAX PLUS ONE, NOT COUNT PLUS ONE. A tab somebody has deleted rows from
        has a count lower than its highest serial, and count+1 would hand out a
        number that is already in the sheet.
        """
        if "sr_no" not in (tab.canonical_role_to_col or {}):
            return ""
        best = 0
        for row in (tab.rows or []):
            raw = clean_cell(row.get("sr_no")).strip()
            try:
                best = max(best, int(float(raw)))
            except (TypeError, ValueError):
                continue
        return str(best + 1)

    def append_row(self, tab: "Tab", values: dict, *, reason: str,
                   expect_company: str = "", dry_run: bool = False) -> dict:
        """Append ONE row. Returns {ok, sheet_row, written, error, remedy, duplicate}.

        THE ONLY WAY A ROW IS EVER CREATED, and it runs only after an approver
        has said yes — `approvals` owns that gate and this function is not
        reachable without it.

        SIX THINGS IN ORDER, and the order is the design:

          1. THE TAB MUST BE APPENDABLE. SHEET_APPENDABLE_TABS decides, because
             "which tabs may grow" is a decision about the workbook rather than
             about one row.
          2. DUPLICATE CHECK. A company (or company + person) already on the tab
             is reported, not appended. Said out loud — a silent skip reads as
             a successful append to everybody downstream.
          3. WHICH COLUMNS. Only MAPPED roles, and on Outreach PoCs only the
             new-row band A:R. S-X is refused on a new row exactly as it is on
             an existing one: a bot that has just discovered a company has no
             business stating its closure probability.
          4. AN EMPTY ROW, CHECKED IMMEDIATELY BEFORE WRITING. Not "the row
             arithmetic said it was empty a moment ago" — re-read, because
             somebody typing into the sheet between the two is exactly the race
             this would lose.
          5. WRITE.
          6. READ BACK AND COMPARE EVERY CELL. On any mismatch the written
             cells are CLEARED and the failure is reported. A half-written row
             is worse than no row: it looks like data.

        `dry_run` (SHEET_WRITES_ENABLED=false) stops after step 4 and reports
        exactly what would have been written.
        """
        out: dict = {
            "ok": False, "sheet_row": 0, "written": [], "error": "", "remedy": "",
            "duplicate": None, "dry_run": bool(dry_run),
        }

        # A SIMULATION NEVER WRITES, whatever SHEET_WRITES_ENABLED says. There
        # is no flag to turn this off and there must not be one: the whole
        # contract of a simulation is that it leaves no trace, and a sheet row
        # is the most visible trace there is.
        try:
            import simulation as _sim
            if _sim.in_simulation():
                dry_run = True
                out["dry_run"] = True
                out["dry_run_reason"] = "a simulation never writes to the sheet"
        except Exception:
            pass

        if not config.SHEET_WRITES_ENABLED and not dry_run:
            # Belt and braces: the caller checks too, but this is the
            # function that opens a socket to the sheet.
            dry_run = True
            out["dry_run"] = True

        appendable = {
            str(t).strip().lower() for t in (config.SHEET_APPENDABLE_TABS or [])
        }
        if tab.kind not in appendable:
            out["error"] = (
                f"{tab.title!r} ({tab.kind}) is not in SHEET_APPENDABLE_TABS, so no "
                "row may be added to it"
            )
            out["remedy"] = "Add its kind to SHEET_APPENDABLE_TABS if it should grow."
            return out

        company = str(values.get("company") or expect_company or "").strip()
        poc = str(values.get("name") or values.get("poc") or values.get("event") or "").strip()
        dup = self.find_duplicate(tab, company=company, poc=poc)
        if dup is not None:
            who = f"{company} · {poc}" if poc else company
            out["duplicate"] = {
                "sheet_row": dup.get("_row"),
                "company": clean_cell(dup.get("company")),
                "poc": clean_cell(dup.get("name") or dup.get("poc") or dup.get("event")),
            }
            out["error"] = (
                f"{who} is already on {tab.title!r} at row {dup.get('_row')}, so I have "
                "not added a second one"
            )
            log.info("[gtm.append] refused a duplicate: %s (row %s)", who, dup.get("_row"))
            return out

        # WHICH CELLS. Mapped roles only, and band-checked per row kind.
        cells: list = []
        refused: list = []
        payload = dict(values)
        sr = self._next_sr_no(tab)
        if sr and "sr_no" not in payload:
            payload["sr_no"] = sr

        for role, value in payload.items():
            text = clean_cell(value)
            if not text:
                continue
            idx = (tab.canonical_role_to_col or {}).get(role)
            if idx is None:
                refused.append({"role": role, "why": f"{tab.title!r} has no {role} column"})
                continue
            if tab.kind == POCS and not config.may_write_new_row_column(idx):
                refused.append({
                    "role": role,
                    "why": (
                        f"column {config.column_label(idx)} is outside "
                        f"NEW_ROW_WRITABLE_RANGES ({config.NEW_ROW_WRITABLE_RANGES}) — "
                        "the commercial block is never written, even on a new row"
                    ),
                })
                continue
            cells.append({
                "role": role, "column": config.column_label(idx), "index": idx,
                "header": tab.headers[idx] if idx < len(tab.headers) else role,
                "value": text,
            })

        out["refused"] = refused
        if not cells:
            out["error"] = "nothing in that row maps to a column I may write"
            return out

        try:
            sh = self._open(ORIGINAL)
            ws = sh.worksheet(tab.title)
        except Exception as e:
            out["error"] = f"the sheet could not be opened ({type(e).__name__})"
            return out

        target = self.first_empty_row(tab, sh)
        if target <= 0:
            out["error"] = "I could not work out where the tab ends"
            return out
        out["sheet_row"] = target

        # THE ROW MUST BE EMPTY, RE-READ NOW. Not "the arithmetic said so a
        # moment ago" — somebody typing into the sheet between the two is
        # exactly the race this would otherwise lose.
        try:
            existing = ws.row_values(target)
        except Exception:
            existing = []
        occupied = [
            f"{config.column_label(i)}={str(v).strip()!r}"
            for i, v in enumerate(existing or []) if str(v).strip()
        ]
        if occupied:
            out["error"] = (
                f"row {target} is not empty ({', '.join(occupied[:4])}), so I have not "
                "written anything"
            )
            out["remedy"] = "Somebody may have added a row since I last read the tab."
            log.warning("[gtm.append] refused: row %d on %r is occupied", target, tab.title)
            return out

        if dry_run:
            out["ok"] = True
            out["written"] = [dict(c, old="") for c in cells]
            log.info(
                "[gtm.append] DRY RUN — would write %d cell(s) into row %d of %r: %s",
                len(cells), target, tab.title,
                ", ".join(f"{c['column']}={c['value']!r}" for c in cells),
            )
            return out

        # WRITE, one batch.
        try:
            ws.batch_update([
                {"range": f"{c['column']}{target}", "values": [[c["value"]]]}
                for c in cells
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            # A 403 HERE IS A SHARING PROBLEM, NOT A BUG, and it is worth saying
            # so in those words. The service account can READ the playbook — it
            # just read 517 rows out of it — and "the caller does not have
            # permission" on a write means it was shared as VIEWER. That is one
            # click to fix and impossible to guess from the raw error.
            detail = str(e)
            if "403" in detail or "permission" in detail.lower():
                out["error"] = (
                    "the sheet refused the write: the service account can read this "
                    "playbook but not write to it"
                )
                out["remedy"] = (
                    f"Share the spreadsheet with {config.service_account_email() or 'the service account'} "
                    "as an EDITOR (it currently has Viewer). Every write path is "
                    "affected, not just row additions."
                )
                log.error(
                    "[gtm.append] 403 on %r row %d — the service account has read "
                    "access but not write access. Share the playbook as Editor.",
                    tab.title, target,
                )
            else:
                out["error"] = f"the sheet refused the write ({type(e).__name__}: {e})"
                log.exception("[gtm.append] the write failed on %r row %d",
                              tab.title, target)
            return out

        # READ BACK AND COMPARE EVERY CELL. A write that reported success and
        # landed somewhere else is the failure this exists to catch — and the
        # only way to catch it is to look.
        try:
            back = ws.row_values(target)
        except Exception as e:
            out["error"] = (
                f"the row was written but could not be read back to confirm "
                f"({type(e).__name__}). I have left it in place rather than clearing "
                "something I cannot see."
            )
            return out

        mismatches = []
        for c in cells:
            got = clean_cell(back[c["index"]]) if c["index"] < len(back) else ""
            if normalise_header(got) != normalise_header(c["value"]):
                mismatches.append(f"{c['column']}: wrote {c['value']!r}, read {got!r}")

        if mismatches:
            log.error(
                "[gtm.append] READ-BACK MISMATCH on %r row %d: %s. Clearing what was "
                "written.", tab.title, target, "; ".join(mismatches),
            )
            try:
                ws.batch_update([
                    {"range": f"{c['column']}{target}", "values": [[""]]}
                    for c in cells
                ], value_input_option="USER_ENTERED")
                cleared = "the cells have been cleared"
            except Exception:
                log.exception("[gtm.append] could not clear the mismatched row")
                cleared = (
                    "I could NOT clear them — row %d of %r needs a human eye"
                    % (target, tab.title)
                )
            out["error"] = (
                "the row did not read back as written (" + "; ".join(mismatches[:3])
                + "), so " + cleared
            )
            return out

        out["ok"] = True
        out["written"] = cells
        log.info(
            "[gtm.append] wrote and verified %d cell(s) into row %d of %r (%s)",
            len(cells), target, tab.title, reason,
        )
        return out

    def clear_cells(self, tab_title: str, *, row: int, columns: list,
                    reason: str) -> dict:
        """Blank specific cells on one row. How an appended row is undone.

        IT NEVER DELETES A ROW. Deleting shifts every row below it, which would
        renumber cells other people's notes and formulas point at — and the undo
        for an append is "make it as if nothing was written", not "make the
        sheet one row shorter". An undone append leaves an empty row, and the
        next append reuses it.
        """
        out = {"ok": False, "cleared": [], "error": ""}
        try:
            sh = self._open(ORIGINAL)
            ws = sh.worksheet(tab_title)
            ws.batch_update([
                {"range": f"{col}{int(row)}", "values": [[""]]} for col in columns
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            out["error"] = f"the cells could not be cleared ({type(e).__name__}: {e})"
            log.exception("[gtm.append] undo failed on %r row %s", tab_title, row)
            return out
        out["ok"] = True
        out["cleared"] = list(columns)
        log.info("[gtm.append] undo: cleared %s on %r row %s (%s)",
                 ", ".join(columns), tab_title, row, reason)
        return out

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

        # A SIMULATION NEVER WRITES A CELL EITHER. Same contract as
        # `append_row`: the caller is supposed to have arranged this, and
        # this is the function that opens the socket to the sheet.
        try:
            import simulation as _sim
            if _sim.in_simulation():
                log.info(
                    "[gtm] simulation: refusing to write %d cell(s) to row %s",
                    len(values or {}), row,
                )
                return {
                    "ok": False, "written": [], "error": "", "simulated": True,
                    "remedy": "a simulation never writes to the sheet",
                }
        except Exception:
            pass
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

    def write_cells_on(self, tab: "Tab", *, row: int, values: dict,
                       reason: str = "") -> dict:
        """Write cells on a row of a NON-canonical tab. Returns the same shape
        as `write_cells`.

        WHY A SECOND METHOD RATHER THAN A `tab=` ARGUMENT ON THE FIRST.
        `write_cells` carries the Outreach PoCs tab's own safety furniture — the
        restricted-band refusal (S:X is the humans' territory) and the
        company-name interlock that proves the row is still the row somebody
        approved. Neither has any meaning on the Events tab, and threading a tab
        through the original would have meant `if tab is pocs` around both, with
        the failure mode that a future caller passing another tab silently loses
        the band check on the one tab that needs it.

        SO THIS IS DELIBERATELY NARROWER: it writes to tabs on the allow-list
        below, one named cell at a time, only into MAPPED columns, and it
        refuses to overwrite a cell that already has something in it. That last
        rule is what makes it safe without the interlock — this method can add a
        fact the sheet was missing and can never replace one a human typed.

        It is reachable only from an approved proposal, like every other write.
        """
        out: dict = {"ok": False, "written": [], "refused": [], "error": "",
                     "dry_run": False}

        # A SIMULATION NEVER WRITES, whatever else is true. Same rule and same
        # reason as `append_row`.
        try:
            import simulation as _sim
            if _sim.in_simulation():
                out["dry_run"] = True
        except Exception:
            pass
        if not config.SHEET_WRITES_ENABLED:
            out["dry_run"] = True

        if tab is None:
            out["error"] = "there is no such tab to write to"
            return out
        # THE SAME ALLOW-LIST THE APPEND USES. A tab nobody said could grow is
        # also a tab nobody said could be edited by the bot.
        appendable = {str(t).strip().lower()
                      for t in (config.SHEET_APPENDABLE_TABS or [])}
        if tab.kind not in appendable:
            out["error"] = (
                f"{tab.title!r} ({tab.kind}) is not in SHEET_APPENDABLE_TABS, so I "
                "will not write to it"
            )
            return out

        current = next((r for r in tab.rows if r.get("_row") == int(row)), None)
        if current is None:
            out["error"] = f"row {int(row)} is not on {tab.title!r} any more"
            return out

        planned: list = []
        for role, new_value in (values or {}).items():
            idx = tab.role_to_col.get(role)
            if idx is None:
                out["refused"].append({
                    "role": role, "column": "",
                    "why": f"{tab.title!r} has no column mapped to {role!r}",
                })
                continue
            old = clean_cell(current.get(role))
            if old:
                # NEVER OVERWRITE. This method exists to fill a gap the sheet
                # has; a cell with something already in it is somebody's answer,
                # and replacing it is a different and much riskier operation
                # than the one that was approved.
                out["refused"].append({
                    "role": role, "column": _col_letter(idx),
                    "why": (f"{_col_letter(idx)}{int(row)} already reads {old!r} — I "
                            "only fill blanks on this tab, I never overwrite"),
                })
                continue
            planned.append({
                "role": role,
                "column": _col_letter(idx),
                "cell": f"{_col_letter(idx)}{int(row)}",
                "header": tab.headers[idx] if idx < len(tab.headers) else role,
                "old": "",
                "new": str(new_value or "").strip(),
            })

        if not planned:
            out["error"] = out["error"] or (
                "; ".join(r["why"] for r in out["refused"])
                or "nothing writable in that update"
            )
            return out

        if out["dry_run"]:
            out["written"] = planned
            out["ok"] = True
            log.info("[gtm] DRY RUN: would write %s on %s",
                     ", ".join(p["cell"] for p in planned), tab.title)
            return out

        try:
            sh = self._open()
            ws = sh.worksheet(tab.title)
            for entry in planned:
                # One cell per call, by construction, exactly as `write_cells`
                # does it: a bug in a range string cannot reach a neighbour.
                ws.update(
                    values=[[entry["new"]]],
                    range_name=entry["cell"],
                    value_input_option="USER_ENTERED",
                )
                out["written"].append(entry)
                log.info(
                    "[gtm] WROTE %s on %s (%s) -> %r%s",
                    entry["cell"], tab.title, entry["header"], entry["new"],
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
