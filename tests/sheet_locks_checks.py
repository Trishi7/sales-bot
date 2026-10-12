"""THE WRITE LOCK, BY ROLE (12 Oct 2026) — the checks, shared by verify_sheet_locks.py and
tests/test_sheet_locks.py.

    sys.path.insert(0, <repo>/tests); import sheet_locks_checks as slc
    slc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: header rows through the real sheet parser (which is where the lock is
resolved), the real lock (`config.resolve_column_lock`), and the real write paths (`gtm_sheet.write_cells`,
`write_email`, `append_row`) over a stand-in worksheet that records every cell it is asked to write. No network,
no real sheet. The lock settings are pinned to their .env.example defaults, never read from this machine's .env
(which still carries the old letter ranges).

THREE HEADER ORDERS:
  OLD       the 7 Oct 2026 order (tests/canned_sheet.POCS_HEADERS).
  NEW       Next Steps after LI Connected Date, then the three email pairs, then LI DM Sent and LI DM Date. THIS
            IS AN INFERENCE from "Next Steps is now O, LI DM Sent and LI DM Date are V and W", built here AS A
            FIXTURE ONLY. Nothing in the product code knows it.
  SHUFFLED  the same headers in an arbitrary fixed order.
"""
import asyncio
import contextlib
import logging
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402
import deadlines as dl  # noqa: E402
import gtm_sheet  # noqa: E402
import rule13_fixtures as fx  # noqa: E402

OLD = list(fx.HEADERS_7OCT)
_MOVED = ("Next Steps", "1st Email Sent", "1st Email Date", "2nd Email Sent", "2nd Email Date",
          "3rd Email Sent", "3rd Email Date", "LI DM Sent", "LI DM Date")
_HEAD = [h for h in OLD[:OLD.index("Meeting Date")] if h not in _MOVED]
NEW = _HEAD + list(_MOVED) + OLD[OLD.index("Meeting Date"):]
SHUFFLED = list(OLD)
random.Random(20261012).shuffle(SHUFFLED)

IDENTITY, STEP, COMMERCIAL = list(config.IDENTITY_ROLES), list(config.STEP_ROLES), list(config.COMMERCIAL_ROLES)
WRITABLE_ROLES = ["first_contact", "first_contact_type", "first_contact_date", "sid_li_added",
                  "li_connected_date", "meeting_date", "meeting_status"]
WRITABLE_HEADERS = ["First Contact", "First Contact Type", "First Contact Date", "Sid - LI Addition",
                    "LI Connected Date", "Meeting Date", "Meeting Status"]
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def pins() -> dict:
    return {"RESTRICTED_COLUMN_ROLES": [r for r in _example("RESTRICTED_COLUMN_ROLES").split(",") if r],
            "RESTRICTED_COLUMN_RANGES": _example("RESTRICTED_COLUMN_RANGES"),
            "NEW_ROW_WRITABLE_ROLES": [r for r in _example("NEW_ROW_WRITABLE_ROLES").split(",") if r],
            "NEW_ROW_WRITABLE_RANGES": _example("NEW_ROW_WRITABLE_RANGES"),
            "SHEET_WRITES_ENABLED": True, "SHEET_ROW_ADDITIONS_ENABLED": True, "EMAIL_WRITE_ALLOWED": True,
            "SHEET_APPENDABLE_TABS": ["outreach_pocs", "researcher_lines", "events_summits"]}


@contextlib.contextmanager
def pinned(**extra):
    values = dict(pins(), **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    lock = config.active_lock()
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)
        config.set_active_lock(lock)


def row_for(headers, **by_header) -> list:
    cells = {"Sr No": "1", "Company/Uni": "Acme Labs", "Name": "Priya Rao", "Designation": "CTO"}
    cells.update(by_header)
    return [cells.get(h, "") for h in headers]


def tab_for(headers, **by_header):
    """Parse one row under `headers`. PARSING IS WHAT RESOLVES THE LOCK and puts it in force."""
    return fx.parse([row_for(headers, **by_header)], headers)


def letters(indexes) -> str:
    return config._runs_label(indexes)


def header_of(tab, role) -> str:
    return tab.headers[tab.role_to_col[role]]


class Ws:
    """A worksheet that records every cell it is asked to write."""

    def __init__(self, grid=None):
        self.updates, self.batches, self.grid = [], [], grid or []
        self.title = "Outreach PoCs"

    def update(self, values=None, range_name="", value_input_option=""):
        self.updates.append((range_name, values[0][0] if values else None))

    def batch_update(self, *a, **k):
        self.batches.append(a)

    def get_all_values(self):
        return self.grid

    def row_values(self, n):
        return self.grid[n - 1] if 0 < n <= len(self.grid) else []


@contextlib.contextmanager
def writing(tab, ws):
    """The real write paths over `tab` and the recording worksheet."""
    S = gtm_sheet.SHEETS
    saved = {k: S.__dict__.get(k, _ABSENT) for k in ("read", "_open", "_refuse_if_read_only")}

    def read(*a, **k):
        config.set_active_lock(tab.lock)             # a real read parses, and parsing resolves the lock
        return {gtm_sheet.POCS: tab}
    S.read = read
    S._open = lambda *a, **k: type("Sh", (), {"worksheet": lambda s, t: ws,
                                              "batch_update": lambda s, *a, **k: ws.batches.append(a)})()
    S._refuse_if_read_only = lambda *a, **k: ""
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _ABSENT:
                S.__dict__.pop(k, None)
            else:
                setattr(S, k, v)


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelname, record.getMessage()))


@contextlib.contextmanager
def tapped():
    tap, logger = LogTap(), logging.getLogger()
    was = logger.level
    logger.addHandler(tap)
    logger.setLevel(logging.INFO)
    getattr(gtm_sheet.SHEETS, "_lock_failures_logged", set()).clear()
    try:
        yield tap
    finally:
        logger.removeHandler(tap)
        logger.setLevel(was)


# -- the checks ---------------------------------------------------------------------------------------------------------

def resolving(check):
    print("\nRESOLVING — the same roles are locked wherever their columns sit")
    old = tab_for(OLD)
    check("OLD order: the identity block, the step block, the commercial block and Priority are restricted. "
          "In letters: A:I, then O:W (the old Q:W plus LI DM Sent and LI DM Date at O and P, which the step "
          "block now includes), then Z:AF (the old Z:AE plus Priority at AF)",
          (letters(old.lock.restricted), old.lock.unmapped), ("A:I, O:W, Z:AF", ()))
    check("...the old letter bands A:I, Q:W and Z:AE are all inside it, and AF is no longer unlocked",
          ([i for i in list(range(0, 9)) + list(range(16, 23)) + list(range(25, 31)) if i not in old.lock.restricted],
           config.is_restricted_column(31)), ([], True))
    check("...writable on an existing row: exactly the first-contact, connection and meeting cells",
          ([old.lock.role_of[i] for i in old.lock.writable()], config.writable_window_label()),
          (WRITABLE_ROLES, "J:N, X:Y"))

    new = tab_for(NEW)
    check("NEW order (a FIXTURE of the inferred layout): Next Steps is at O and 1st Email Sent at P",
          (config.column_label(new.role_to_col["outreach_step"]), config.column_label(new.role_to_col["email_1_sent"]),
           config.column_label(new.role_to_col["li_dm_sent"]), config.column_label(new.role_to_col["li_dm_date"])),
          ("O", "P", "V", "W"))
    check("...and Next Steps, 1st Email Sent and every other step-block role resolve as RESTRICTED",
          [r for r in STEP if not config.is_restricted_column(new.role_to_col[r])], [])
    check("...the writable set is exactly First Contact, First Contact Type, First Contact Date, Sid - LI "
          "Addition, LI Connected Date, Meeting Date, Meeting Status",
          ([new.headers[i] for i in new.lock.writable()], config.writable_roles()),
          (WRITABLE_HEADERS, WRITABLE_ROLES))
    check("...where the old LETTER bands would have left Next Steps and 1st Email Sent writable",
          [h for h in ("Next Steps", "1st Email Sent") if 9 <= NEW.index(h) <= 15], ["Next Steps", "1st Email Sent"])

    shuffled = tab_for(SHUFFLED)
    check("SHUFFLED order: the same roles are still locked, by header",
          (sorted(shuffled.headers[i] for i in shuffled.lock.restricted),
           sorted(shuffled.headers[i] for i in shuffled.lock.writable())),
          (sorted(h for h in OLD if h not in WRITABLE_HEADERS), sorted(WRITABLE_HEADERS)))
    check("...and every restricted role still maps", shuffled.lock.unmapped, ())
    check("before any tab has been read nothing may be written at all",
          (_with_lock(None, lambda: config.is_restricted_column(9)), bool(_with_lock(None, config.lock_failure))),
          (True, True))
    src = "".join(open(os.path.join(ROOT, f), encoding="utf-8").read() for f in ("config.py", "gtm_sheet.py"))
    code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
    check("no letter band is hardcoded as a default any more",
          [m for m in re.findall(r'or "([A-Z]{1,2}:[A-Z]{1,2}(?:,[A-Z]{1,2}:[A-Z]{1,2})*)"', code)], [])


def _with_lock(lock, fn):
    saved = config.active_lock()
    config.set_active_lock(lock)
    try:
        return fn()
    finally:
        config.set_active_lock(saved)


def failing_closed(check):
    print("\nFAILING CLOSED — a restricted role with no column refuses every write")
    headers = [h if h != "Next Steps" else "Column the bot cannot name" for h in NEW]
    with tapped() as tap:
        tab = tab_for(headers)
    check("the Next Steps header is renamed: that role maps to no column", tab.lock.unmapped, ("outreach_step",))
    check("...it is logged at ERROR, naming the role",
          [("outreach_step" in m and "REFUSED" in m) for lvl, m in tap.lines if lvl == "ERROR"
           and "[gtm.lock]" in m], [True])
    ws = Ws()
    with writing(tab, ws), tapped() as tap2:
        cells = gtm_sheet.SHEETS.write_cells(row=2, values={"meeting_date": "21-10-2026"},
                                             expect_company="Acme Labs")
        email = gtm_sheet.SHEETS.write_email(row=2, email="priya@acme.example", expect_company="Acme Labs",
                                             expect_name="Priya Rao")
        added = gtm_sheet.SHEETS.append_row(tab, {"company": "Nova Labs", "name": "Someone New"}, reason="check",
                                            dry_run=True)
    check("EVERY write to the tab is refused: a writable cell, the Email cell, and a new row",
          (cells["ok"], email["ok"], added["ok"], ws.updates, ws.batches), (False, False, False, [], []))
    check("...each refusal says which role did not map",
          ["outreach_step" in str(x) for x in (cells.get("error") or cells.get("refused"), email["error"],
                                                added["error"])], [True, True, True])
    check("...and each is logged at ERROR", len([m for lvl, m in tap2.lines if lvl == "ERROR"
                                                 and "REFUSING" in m]) >= 3, True)
    fixed = tab_for(NEW)
    ws = Ws()
    with writing(fixed, ws):
        out = gtm_sheet.SHEETS.write_cells(row=2, values={"meeting_date": "21-10-2026"}, expect_company="Acme Labs")
    check("once the header is back the lock resolves again and a writable cell is written",
          (out["ok"], ws.updates), (True, [("X2", "21-10-2026")]))


def ranges_and_new_rows(check):
    print("\nLETTER RANGES ONLY ADD — and a new row never opens the step or commercial block")
    tab_for(NEW)
    base = set(config.active_lock().restricted)
    with pinned(RESTRICTED_COLUMN_RANGES="X:Y"):
        ranged = tab_for(NEW)
        check("RESTRICTED_COLUMN_RANGES=X:Y ADDS the two meeting columns to the lock",
              (sorted(set(ranged.lock.restricted) - base), config.writable_roles()),
              ([23, 24], WRITABLE_ROLES[:5]))
        check("...and takes nothing away: every role-locked column is still locked",
              sorted(base - set(ranged.lock.restricted)), [])
    with pinned(RESTRICTED_COLUMN_RANGES="A:C"):
        narrow = tab_for(NEW)
        check("a range that names fewer columns than the roles unlocks nothing (the lock is the UNION)",
              sorted(base - set(narrow.lock.restricted)), [])
    with pinned(RESTRICTED_COLUMN_ROLES=IDENTITY + COMMERCIAL, RESTRICTED_COLUMN_RANGES="O:W"):
        byletter = tab_for(NEW)
        check("a range can lock a column no role names",
              [r for r in STEP if byletter.role_to_col[r] not in byletter.lock.restricted], [])

    for name, headers in (("OLD", OLD), ("NEW", NEW), ("SHUFFLED", SHUFFLED)):
        tab = tab_for(headers)
        opened = sorted(tab.headers[i] for i in tab.lock.new_row)
        check(f"{name} order: a new row may fill the identity block and the first-contact and connection "
              "cells, and nothing else",
              opened, sorted(header_of(tab, r) for r in IDENTITY + WRITABLE_ROLES[:5]))
    greedy = pins()["NEW_ROW_WRITABLE_ROLES"] + STEP + COMMERCIAL
    with pinned(NEW_ROW_WRITABLE_ROLES=greedy, NEW_ROW_WRITABLE_RANGES="A:AF"):
        tab = tab_for(NEW)
        check("NEW_ROW_WRITABLE_ROLES naming every step and commercial role, and NEW_ROW_WRITABLE_RANGES=A:AF: "
              "not one step-block or commercial-block column is opened",
              [r for r in STEP + COMMERCIAL if config.may_write_new_row_column(tab.role_to_col[r])], [])
        check("...they are named for the boot warning",
              (tab.lock.ignored_new, bool(config.new_row_locked_overlap())), (tuple(STEP + COMMERCIAL), True))
    tab = tab_for(NEW)
    ws = Ws([list(NEW), row_for(NEW)])
    with writing(tab, ws):
        out = gtm_sheet.SHEETS.append_row(
            tab, {"company": "Nova Labs", "name": "Someone New", "outreach_step": "Send email 1",
                  "email_1_sent": "yes", "li_dm_sent": "Sent", "next_steps": "hello", "closure_prob": "80%",
                  "poc_priority": "P1"}, reason="check", dry_run=True)
    check("a new row with step and commercial values in it: those are refused, the identity cells are kept",
          (sorted(r["role"] for r in out.get("refused") or []),
           sorted(c["role"] for c in out.get("written") or [] if c["role"] != "sr_no")),
          (sorted(["outreach_step", "email_1_sent", "li_dm_sent", "next_steps", "closure_prob", "poc_priority"]),
           ["company", "name"]))


def the_two_exceptions(check):
    print("\nTHE TWO EXCEPTIONS — a blank Email cell after a yes, and a new row")
    for name, headers in (("NEW", NEW), ("SHUFFLED", SHUFFLED)):
        tab = tab_for(headers)
        cell = f"{config.column_label(tab.role_to_col['email'])}2"
        ws = Ws()
        with writing(tab, ws):
            out = gtm_sheet.SHEETS.write_email(row=2, email="priya@acme.example", expect_company="Acme Labs",
                                               expect_name="Priya Rao")
            plain = gtm_sheet.SHEETS.write_cells(row=2, values={"email": "x@acme.example"},
                                                 expect_company="Acme Labs")
        check(f"{name} order: the Email cell is written through the email_write path when it is blank, and "
              "only that cell", (out["ok"], ws.updates), (True, [(cell, "priya@acme.example")]))
        check(f"{name} order: ...and the ordinary write path still refuses it",
              (plain["ok"], [r["role"] for r in plain.get("refused") or []]), (False, ["email"]))
    filled = tab_for(NEW, **{"Email id": "already@acme.example"})
    ws = Ws()
    with writing(filled, ws):
        out = gtm_sheet.SHEETS.write_email(row=2, email="priya@acme.example", expect_company="Acme Labs",
                                           expect_name="Priya Rao")
    check("an Email cell that already holds a value is left alone", (out["ok"], bool(out["skipped"]), ws.updates),
          (False, True, []))
    tab = tab_for(NEW)
    ws = Ws()
    with pinned(EMAIL_WRITE_ALLOWED=False), writing(tab, ws):
        out = gtm_sheet.SHEETS.write_email(row=2, email="priya@acme.example", expect_company="Acme Labs",
                                           expect_name="Priya Rao")
    check("with EMAIL_WRITE_ALLOWED=false it is refused", (out["ok"], ws.updates), (False, []))

    import replies_world as rw

    async def row_add():
        with rw.World(False, pins=dict(pins())) as w:
            tab = tab_for(NEW)
            calls = []

            def append_row(tab_, values, **kw):
                calls.append((dict(values), dict(kw)))
                return {"ok": True, "sheet_row": 9, "written": [], "signed": True, "dry_run": False, "error": ""}
            gtm_sheet.SHEETS.append_row = append_row
            person = {"company": "Nova Labs", "name": "Someone New",
                      "linkedin_url": "https://www.linkedin.com/in/someone-new"}
            ok = await w.bot._write_poc_row(tab, person, reason="check", approver="Vaishnavi",
                                            approval_link="https://discord.test/1/2")
            none = await w.bot._write_poc_row(tab, person, reason="check", approver="Vaishnavi", approval_link="")
            return calls, ok, none

    with pinned():
        calls, ok, unsigned = asyncio.run(row_add())
    values, kw = calls[0] if calls else ({}, {})
    check("a row_add fills only Name, Company and the LinkedIn URL",
          sorted(k for k, v in values.items() if str(v).strip()), ["company", "li_url", "name"])
    check("...with the signature in a cell NOTE on the Name cell, never in a data column",
          (kw.get("note_role"), "Added by" in str(kw.get("note_text")), "approved by Vaishnavi" in str(
              kw.get("note_text")), [k for k, v in values.items() if "Added by" in str(v)]),
          ("name", True, True, []))
    check("...and no row at all when the approval cannot be linked", (len(calls), unsigned.get("ok")), (1, False))
    tab = tab_for(NEW)
    check("every cell it fills is one a new row may fill, in the NEW order too",
          [r for r in ("company", "name", "li_url") if not config.may_write_new_row_column(tab.role_to_col[r])], [])


def the_log_and_the_settings(check):
    print("\nTHE STARTUP LOG AND THE SETTINGS — where the lock sits today, in one glance")
    tab = tab_for(NEW)
    with tapped() as tap:
        gtm_sheet.SHEETS.log_writable_window(tab)
    lines = [m for _l, m in tap.lines]
    step = next((m for m in lines if "step block" in m), "")
    check("the log prints, for every restricted role, the LETTER it resolved to today",
          [f"{r}=" in " ".join(lines) for r in IDENTITY + STEP + COMMERCIAL], [True] * 25)
    check("...so the NEW order reads Next Steps at O and LI DM Sent at V",
          ("outreach_step=O 'Next Steps'" in step, "email_1_sent=P '1st Email Sent'" in step,
           "li_dm_sent=V 'LI DM Sent'" in step, "li_dm_date=W 'LI DM Date'" in step), (True, True, True, True))
    window = next((m for m in lines if "[gtm.window]" in m), "")
    check("...and the writable window as both letters and role names",
          ("WRITABLE on an existing row: J:N, X:Y" in window,
           "roles: " + ", ".join(WRITABLE_ROLES) in window), (True, True))
    check("...and what a new row may fill",
          any("a NEW row may fill A:N" in m for m in lines), True)

    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("RESTRICTED_COLUMN_ROLES in .env.example is the three blocks, Priority included",
          pins()["RESTRICTED_COLUMN_ROLES"], IDENTITY + STEP + COMMERCIAL)
    check("the letter ranges default to empty, in the code and in .env.example",
          (pins()["RESTRICTED_COLUMN_RANGES"], pins()["NEW_ROW_WRITABLE_RANGES"],
           'getenv("RESTRICTED_COLUMN_RANGES", "") or "").strip()\n' in src,
           'getenv("NEW_ROW_WRITABLE_RANGES", "") or "").strip()\n' in src), ("", "", True, True))
    check("NEW_ROW_WRITABLE_ROLES is the identity block plus the first-contact and connection cells",
          pins()["NEW_ROW_WRITABLE_ROLES"], IDENTITY + WRITABLE_ROLES[:5])
    claude = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
    check("CLAUDE.md states the rule by block, not by letter",
          ([w for w in ("A–I", "Q–W", "Z–AE", "J–P", "X–Y", "A–P") if w in claude],
           [w for w in ("RESTRICTED_COLUMN_ROLES", "NEW_ROW_WRITABLE_ROLES", "email_write", "row_add",
                        "EMAIL_WRITE_ALLOWED") if w not in claude]), ([], []))


def run(check):
    with pinned():
        resolving(check)
        failing_closed(check)
        ranges_and_new_rows(check)
    with pinned():
        the_two_exceptions(check)
        the_log_and_the_settings(check)
