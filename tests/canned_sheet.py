"""CANNED SHEET ROWS for the offline checks that used to read the REAL GTM spreadsheet.

WHY. `gtm_sheet.SHEETS.read()` opens the real spreadsheet (read-only). Four verify scripts and one
pytest case did that in their "offline" mode without anyone noticing (the offline guard,
tests/offline_guard.py, now blocks it). A check should test its own logic on rows it controls, so this
module replaces `SHEETS.read` with an in-memory read: the same `{kind: Tab}` dict the real one returns,
parsed by the real `_parse_values` from rows shaped like the real tabs (the conftest headers). Made-up
companies and people only (Acme AI, Globex, Ada Lovelace, ...), never real prospects.

    sys.path.insert(0, <repo>/tests); import canned_sheet; canned_sheet.install()

`install()` is idempotent; `uninstall()` restores the real method. It does not touch Drive, the to-do
sheet, or `_rule_tab_rows` (a script that fakes those keeps doing so). It is NOT used where the point is
the live sheet (verify_poc_lookup, verify_layouts, verify_simulation, the live modes).
"""
# The real tabs' headers, copied from tests/conftest.py (not imported: a script must not load pytest's conftest).
POCS_HEADERS = [
    "Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
    "Based", "Research Paper Link", "LI Url", "First Contact",
    "First Contact Type", "First Contact Date", "Sid - LI Addition",
    "LI Connected Date", "LI DM Sent", "LI DM Date", "Meeting Date",
    "Meeting Status", "Next Steps/Notes", "Package", "Prospect Status",
    "Closure Prob%", "Estd. Deal Size (USD)", "Deal Status",
]

PIPELINE_HEADERS = [
    "Sr no.", "Company", "Industry", "Geography", "Approx. Funding",
    "Outreach Line - Researchers", "Dates",
]

_ORIGINAL = {}


def _blank(n):
    return [""] * n


def pocs_values():
    def row(n, company, industry, name, title, based):
        r = _blank(len(POCS_HEADERS))
        r[0], r[1], r[2], r[3], r[4], r[6] = str(n), company, industry, name, title, based
        return r

    return [list(POCS_HEADERS),
            row(1, "Acme AI", "AI Voice Agents", "Ada Lovelace", "CTO", "SF"),
            row(2, "Acme AI", "AI Voice Agents", "Sam Lee", "Head of Research", "SF"),
            row(3, "Globex", "AI Voice Agents", "Grace Hopper", "Co-Founder", "London"),
            row(4, "Initech", "AI Infrastructure", "Alan Turing", "VP Engineering", "Bengaluru")]


def pipeline_values():
    return [list(PIPELINE_HEADERS),
            ["1", "Acme AI", "AI Voice Agents", "US", "$10M", "outreach line", ""],
            ["2", "Globex", "AI Voice Agents", "UK", "$20M", "outreach line", ""],
            ["3", "Initech", "AI Infrastructure", "IN", "$5M", "outreach line", ""]]


def tabs():
    import deadlines as dl
    import gtm_sheet

    now = dl.real_epoch()
    sheets = gtm_sheet.SHEETS
    return {gtm_sheet.POCS: sheets._parse_values("Outreach PoCs", pocs_values(), read_at=now),
            gtm_sheet.PIPELINE: sheets._parse_values("Master Pipeline", pipeline_values(), read_at=now)}


def install():
    import gtm_sheet

    if "read" in _ORIGINAL:
        return
    _ORIGINAL["read"] = gtm_sheet.SHEETS.read

    def read(which=gtm_sheet.ORIGINAL, force=False, **_kw):
        return tabs()

    gtm_sheet.SHEETS.read = read

    import mapping_sheet

    _ORIGINAL["mapping_read"] = mapping_sheet.MAPPING.read
    # The researcher-mapping sheet: an empty mapping (no tabs), which is what "nothing mapped yet" is.
    mapping_sheet.MAPPING.read = lambda force=False, **_kw: {}


def uninstall():
    if "read" in _ORIGINAL:
        import gtm_sheet

        gtm_sheet.SHEETS.read = _ORIGINAL.pop("read")
        import mapping_sheet

        mapping_sheet.MAPPING.read = _ORIGINAL.pop("mapping_read")
