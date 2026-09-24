"""ROW ACTIVATION — which rows of the canonical tab the bot may raise unprompted.

THE RULE, in one sentence: a row on the "Outreach PoCs" tab is ACTIVE only when a
FIRST-CONTACT DATE or a CONNECTION DATE is present in it, and an INACTIVE row is
invisible to every PROACTIVE feature — never mentioned, never chased, never
counted.

WHY IT EXISTS. The canonical tab is a working list of people somebody might
contact, not a list of people somebody has contacted. Most of it is research: a
name, an organisation, a designation, and nothing else. A bot that chases those
rows is not chasing outreach, it is chasing a spreadsheet — and it drowns the
three rows that are actually in flight under two hundred that were never started.
The dates are the sheet's own record of "this one is real", so they are the
signal, and nothing else is.

PROACTIVE, NOT PRIVATE. This is a rule about what the bot brings up ON ITS OWN
INITIATIVE. Ask about an inactive row by name and you get the whole row: the
lookup tools read the tab unfiltered, because "we have no record of contacting
them" is a real answer to a real question and hiding the row would make the bot
lie about what it can see. What the rule governs is the digest, the counts, the
reminders and every other line the bot writes without being asked.

THE ONE EXCEPTION IS AN EXPLICIT MENTION-REQUEST. "Set connection reminders for
the others at Acme" is a person deciding those rows are real, which is exactly
the judgement the date columns were standing in for. Rows named that way are
ACTIVATED and stay activated: the activation is persisted in SQLite
(`db.activate_row`), because an activation that evaporated on the next restart
would make the bot forget an instruction it was given out loud.

IDENTITY IS COMPANY + PoC, NEVER THE SHEET ROW NUMBER. Row numbers move the
moment somebody sorts the tab, and an activation that followed a row number
would silently transfer to whoever landed in that row next.

THIS MODULE IS PURE. Rows in, verdicts out. It reads no sheet, touches no
database and sends nothing — the persisted activations are passed IN as a set of
keys, so the whole rule is testable without a database.
"""
import logging
from typing import Optional

import gtm_sheet

log = logging.getLogger(__name__)

# The two columns that make a row active. Taken from gtm_sheet so the schema
# layer and this rule cannot drift apart — that module's alias lists are what
# decide which discovered header becomes `first_contact_date` or
# `li_connected_date`.
ACTIVATION_ROLES = gtm_sheet.ACTIVATION_ROLES

# How each role reads in a sentence, for the lines that have to explain
# themselves rather than assert.
ROLE_LABELS = {
    "first_contact_date": "first-contact date",
    "li_connected_date": "LinkedIn connected date",
    # The tracker-era spellings, kept so a label lookup during the migration
    # returns a phrase rather than a raw role name. gtm_sheet.POCS_COMPAT_ALIASES
    # is what still resolves them to a column.
    "first_contacted": "first-contact date",
    "connected": "LinkedIn connected date",
}


def row_key(row: dict) -> str:
    """The stable identity of a row: normalised company | normalised name.

    NEVER the sheet row number. Sorting the tab renumbers every row, and an
    activation keyed on a number would quietly move to a different person.

    READS THE CANONICAL ROLE FIRST, then the retired spelling. `name` is what
    the Outreach PoCs tab calls column D; `poc` is the tracker-era alias that
    `gtm_sheet.POCS_COMPAT_ALIASES` still fills on rows parsed from a sheet.

    THE ORDER MATTERS MORE THAN IT LOOKS. This key is what the dedup ledger,
    the snooze table, the explicit activations and the R9 ladder are all
    written against. Reading only the alias worked on parsed rows and produced
    "company|" — the SAME KEY FOR EVERY CONTACT AT ONE COMPANY — on any row
    built without the shim. Three colleagues at one account would have
    collapsed into one identity, and the dedup would have silently dropped two
    of them.
    """
    company = gtm_sheet.normalise_header(gtm_sheet.clean_cell(row.get("company")))
    poc = gtm_sheet.normalise_header(
        gtm_sheet.clean_cell(row.get("name") or row.get("poc"))
    )
    return f"{company}|{poc}"


def activating_date(row: dict) -> tuple[Optional[object], str]:
    """The date that makes this row active, and which column it came from.

    Returns (date, role) — or (None, "") when neither column holds a readable
    date. First contact is checked first only so the reason reads naturally; a
    row with both is active either way.

    A CELL THAT IS NOT A DATE DOES NOT ACTIVATE. "Yes" in a connection column is
    somebody's shorthand, not a record of when, and the rule as agreed is about a
    DATE being present. `gtm_sheet.sheet_date` already handles the two things
    this sheet actually does — several formats, and cells holding more than one
    date — so a real date in any of the sheet's own formats counts.
    """
    for role in ACTIVATION_ROLES:
        parsed = gtm_sheet.sheet_date(row.get(role))
        if parsed is not None:
            return parsed, role
    return None, ""


def has_activating_date(row: dict) -> bool:
    """Does this row carry a first-contact or connection DATE?"""
    return activating_date(row)[0] is not None


def is_active(row: dict, *, activated_keys=frozenset()) -> bool:
    """Is this row visible to the proactive features?

    Active when it carries an activating date, OR when somebody explicitly
    activated it by name and that activation was persisted.
    """
    if has_activating_date(row):
        return True
    return row_key(row) in (activated_keys or frozenset())


def why_active(row: dict, *, activated_keys=frozenset()) -> str:
    """One phrase saying WHY a row is active, or why it isn't.

    Every line the bot writes about activation quotes this rather than asserting
    a verdict, so "why is this row being chased" is answerable from the message
    itself.
    """
    parsed, role = activating_date(row)
    if parsed is not None:
        return f"{ROLE_LABELS.get(role, role)} {gtm_sheet.clean_cell(row.get(role))!r}"
    if row_key(row) in (activated_keys or frozenset()):
        return "activated by an explicit request"
    return (
        "no first-contact date and no connection date — not started, so it is not "
        "chased, mentioned or counted"
    )


def split(rows: list, *, activated_keys=frozenset()) -> tuple[list, list]:
    """(active, inactive). One pass, so the counts can never disagree."""
    active: list = []
    inactive: list = []
    for row in rows or []:
        (active if is_active(row, activated_keys=activated_keys) else inactive).append(row)
    return active, inactive


def active_rows(rows: list, *, activated_keys=frozenset(), why: str = "") -> list:
    """The rows a proactive feature may see, with the split logged once.

    THE LOG LINE IS THE POINT OF THIS WRAPPER. Filtering silently is how a
    feature ends up with nothing to say and nobody knows whether that is good
    news or a broken column — so every call names the caller and the numbers.
    """
    active, inactive = split(rows, activated_keys=activated_keys)
    log.info(
        "[activation] %s: %d of %d row(s) ACTIVE (%d inactive — no first-contact or "
        "connection date, invisible to proactive output)%s",
        why or "proactive read", len(active), len(rows or []), len(inactive),
        f"; {len(activated_keys)} explicit activation(s) in force" if activated_keys else "",
    )
    if rows and not active:
        log.warning(
            "[activation] %s: NO row on the canonical tab is active. Either nothing has "
            "been contacted yet, or the first-contact / connection date column was "
            "renamed and is no longer being recognised — check the [gtm.schema] lines "
            "for which headers were mapped to %s.",
            why or "proactive read", " / ".join(ACTIVATION_ROLES),
        )
    return active


def matching_rows(rows: list, *, org: str = "", names: Optional[list] = None) -> list:
    """The rows an explicit mention-request names.

    `org` matches the company cell (exact-normalised first, then substring, the
    same way `Tab.find_company` does). `names` narrows to particular PoCs; an
    empty `names` means every row at that org, which is what "set connection
    reminders for the others at <org>" asks for.

    RETURNS ROWS, NOT KEYS, and never invents one: a name that matches nothing
    is simply absent from the result, and the caller reports it as not found
    rather than activating a row that does not exist.
    """
    want_org = gtm_sheet.normalise_header(org)
    pool = list(rows or [])
    if want_org:
        exact = [r for r in pool
                 if gtm_sheet.normalise_header(r.get("company", "")) == want_org]
        pool = exact or [r for r in pool
                         if want_org in gtm_sheet.normalise_header(r.get("company", ""))]
    wanted = [gtm_sheet.normalise_header(n) for n in (names or []) if str(n).strip()]
    if not wanted:
        return pool
    out: list = []
    for row in pool:
        poc = gtm_sheet.normalise_header(row.get("poc", ""))
        if any(w == poc or (w and w in poc) for w in wanted):
            out.append(row)
    return out


def describe(row: dict) -> str:
    """company · PoC (designation) — how an activated row is named back."""
    company = gtm_sheet.clean_cell(row.get("company")) or "(unnamed row)"
    poc = gtm_sheet.clean_cell(row.get("poc"))
    if not poc:
        return company
    designation = gtm_sheet.clean_cell(row.get("poc_designation"))
    return f"{company} · {poc}{f' ({designation})' if designation else ''}"
