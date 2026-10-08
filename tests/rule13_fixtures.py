"""RULE 13 FIXTURES, shared by tests/test_rule13.py, verify_rule13.py and verify_replay_oct6.py.

Written from docs/plans/RULE13.md (sections 4, 5, 13), not from the builder's code.

Everything here is MADE UP (Priya Rao, Acme Labs, "Person 07"...) and parsed by the REAL
`gtm_sheet._parse_values` on the REAL 7 Oct header row, so the roles and the column letters are the
real ones. Nothing here reaches the sheet, Discord or the network.

    sys.path.insert(0, <repo>/tests); import rule13_fixtures as fx

A script must not import pytest's conftest, so the header rows live in canned_sheet (the 7 Oct row)
and are copied here for the pre-7 Oct row.
"""
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canned_sheet  # noqa: E402

HEADERS_7OCT = list(canned_sheet.POCS_HEADERS)
HEADERS_PRE_7OCT = [
    "Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
    "Based", "Research Paper Link", "LI Url", "First Contact",
    "First Contact Type", "First Contact Date", "Sid - LI Addition",
    "LI Connected Date", "LI DM Sent", "LI DM Date", "Meeting Date",
    "Meeting Status", "Next Steps/Notes", "Package", "Prospect Status",
    "Closure Prob%", "Estd. Deal Size (USD)", "Deal Status",
]

# short key -> the 7 Oct header it fills
KEYS = {
    "sr": "Sr No", "company": "Company/Uni", "industry": "Industry", "name": "Name",
    "designation": "Designation", "email": "Email id", "li_url": "LI Url",
    "connected": "Sid - LI Addition", "first_contact": "First Contact", "li_date": "LI Connected Date",
    "dm_sent": "LI DM Sent", "dm_date": "LI DM Date", "step": "Next Steps",
    "e1": "1st Email Sent", "e1d": "1st Email Date", "e2": "2nd Email Sent", "e2d": "2nd Email Date",
    "e3": "3rd Email Sent", "e3d": "3rd Email Date", "meeting_date": "Meeting Date",
    "meeting_status": "Meeting Status", "notes": "Notes/Remarks", "prospect": "Prospect Status",
    "closure": "Closure Prob%", "deal": "Deal Status", "priority": "Priority",
}


def cell(d) -> str:
    """A date as the sheet shows it (DD-MM-YYYY)."""
    return d.strftime("%d-%m-%Y") if isinstance(d, date) else str(d)


def dotted(d) -> str:
    """A date typed as text, DD.MM.YYYY (6 of the 29 dated rows on 7 Oct are like this)."""
    return d.strftime("%d.%m.%Y")


def values_row(n, **cells) -> list:
    """One 7 Oct row (32 cells) by short key. Dates may be `date` objects."""
    r = [""] * len(HEADERS_7OCT)
    r[0] = str(n)
    for key, v in cells.items():
        r[HEADERS_7OCT.index(KEYS[key])] = cell(v)
    return r


def person(n, *, name=None, company=None, **cells) -> list:
    """A Connected, dated person by default."""
    base = {"name": name or f"Person {n:02d}", "company": company or f"Acme Labs {n:02d}",
            "designation": "CTO", "connected": "Connected"}
    base.update(cells)
    return values_row(n, **base)


def parse(rows, headers=None):
    """A parsed Outreach PoCs tab from value rows (header first, added here)."""
    import deadlines as dl
    import gtm_sheet

    return gtm_sheet.SHEETS._parse_values("Outreach PoCs", [list(headers or HEADERS_7OCT)] + [list(r) for r in rows],
                                          read_at=dl.real_epoch())


def tab_of(*people_rows, headers=None):
    return parse(people_rows, headers)


# -- the ten-weekday rotation world: 33 Connected rows, 29 dated, 18 / 11 / 4 -------------------

FIRST_DAY = date(2026, 10, 12)           # Monday
WEEKDAYS = [FIRST_DAY + timedelta(days=d) for d in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11)]
UNDATED_POSITIONS = (3, 10, 18, 30)       # among the 33 Connected rows (0-based): the four with no LI Connected Date


def rotation_values():
    """(value rows, meta). Sheet order, with 5 non-Connected rows mixed in.

    meta["dated"]   the 29 dated Connected people's names in sheet order
    meta["undated"] the four with no date
    meta["steps"]   name -> "email1" | "research" | "email2"
    """
    rng = random.Random(7)
    steps = ["email1"] * 18 + ["research"] * 11 + ["email2"] * 4
    rng.shuffle(steps)
    # the undated four never carry a Send email 2 (that needs a dated first email)
    for pos in UNDATED_POSITIONS:
        if steps[pos] == "email2":
            j = next(i for i, s in enumerate(steps) if s != "email2" and i not in UNDATED_POSITIONS)
            steps[pos], steps[j] = steps[j], steps[pos]
    rows, dated, undated, kinds = [], [], [], {}
    others = {2: "", 9: "Not connected", 15: "Requested", 24: "", 31: "Not connected"}   # sheet positions
    n_dated = e2_index = conn_i = sheet_i = sr = 0
    while conn_i < 33:
        sr += 1
        if sheet_i in others:
            rows.append(person(sr, connected=others[sheet_i], name=f"Other {sr:02d}"))
            sheet_i += 1
            continue
        name = f"Person {sr:02d}"
        step = steps[conn_i]
        cells = {"name": name}
        if conn_i in UNDATED_POSITIONS:
            undated.append(name)
        else:
            c = date(2026, 8, 20) + timedelta(days=n_dated)        # all well over 2 days before 12 Oct
            cells["li_date"] = dotted(c) if n_dated % 5 == 2 else cell(c)     # 6 of the 29 typed as DD.MM.YYYY
            dated.append(name)
            n_dated += 1
        if step == "email1":
            cells["step"] = "Send email 1"
        elif step == "research":
            cells["step"] = "Research the PoC"
        else:
            cells["step"] = "Send email 2"
            if e2_index < 3:
                cells.update(e1="yes", e1d=date(2026, 9, 28) + timedelta(days=e2_index))
            e2_index += 1              # the 4th: Q says email 2 but 1st Email Sent is blank (they disagree)
        kinds[name] = step
        rows.append(person(sr, **cells))
        conn_i += 1
        sheet_i += 1
    return rows, {"dated": dated, "undated": undated, "steps": kinds}


def rotation_tab():
    rows, meta = rotation_values()
    return parse(rows), meta


def edit(row, **cells):
    """Change cells of one value row in place (by short key). Returns the row."""
    for key, v in cells.items():
        row[HEADERS_7OCT.index(KEYS[key])] = cell(v)
    return row
