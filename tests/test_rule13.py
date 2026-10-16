"""RULE 13 (Next Steps follow-ups) and the 7 Oct 2026 sheet layout — offline, from docs/plans/RULE13.md.

Written from the plan and the ticket BEFORE reading the builder's diff. Fixtures are made up
(tests/rule13_fixtures.py), the 7 Oct header row is the real one, every date is pinned, and no test here
reaches a sheet, Discord or a model (tests/offline_guard.py is autouse via conftest).

The bot-level cases (a send through `_send_drip_message`, test day, simulation, replies) live in
verify_rule13.py and verify_replay_oct6.py, which carry a fake Discord.

DEFERRED to NFT2-1063 (not written as passing tests): a "done" reply to a Rule 13 line gets the update-the-sheet
line; two research-step people and an unnamed "done" asks which; a real question in the reply is answered with
the post as context; "consistent with how NFT2-1069 handles fixed-time posts". What holds TODAY (a reply writes
nothing and changes no Rule 13 state) is tested in verify_rule13.py (G1, G2) and in the replay harness.
"""
import logging
import os
import re
from datetime import date, timedelta

import pytest

import config
import deadlines as dl
import gtm_sheet
import nextaction
import rules as rules_mod
import rule13_fixtures as fx

R13 = "R13"
TRIGGER = "next_step_followups"
ASKING = date(2026, 10, 12)            # a Monday


def letter(i):
    return gtm_sheet._col_letter(i)


# ---------------------------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------------------------

IDENTITY = list(config.IDENTITY_ROLES)
STEP = list(config.STEP_ROLES)
COMMERCIAL = list(config.COMMERCIAL_ROLES)
NEW_ROW_DEFAULT = IDENTITY + ["first_contact", "first_contact_type", "first_contact_date", "sid_li_added",
                              "li_connected_date"]


@pytest.fixture
def bands(monkeypatch):
    """Pin the write lock to the contract (BY ROLE since 12 Oct 2026), never to this machine's .env (the laptop
    still carries the old letter ranges), and resolve it against the 7 Oct header order. Call it again to change
    a setting; it resolves again."""
    def pin(restricted_ranges="", new_row_ranges="", new_row_roles=None, restricted_roles=None, headers=None):
        monkeypatch.setattr(config, "RESTRICTED_COLUMN_ROLES",
                            list(restricted_roles if restricted_roles is not None
                                 else IDENTITY + STEP + COMMERCIAL))
        monkeypatch.setattr(config, "RESTRICTED_COLUMN_RANGES", restricted_ranges)
        monkeypatch.setattr(config, "NEW_ROW_WRITABLE_ROLES",
                            list(new_row_roles if new_row_roles is not None else NEW_ROW_DEFAULT))
        monkeypatch.setattr(config, "NEW_ROW_WRITABLE_RANGES", new_row_ranges)
        return fx.parse([fx.values_row(1)], headers)          # parsing the tab resolves the lock
    saved = config.active_lock()
    pin()
    yield pin
    config.set_active_lock(saved)


@pytest.fixture(autouse=True)
def contract_defaults(monkeypatch):
    """The .env.example defaults for every Rule 13 number (the laptop's .env sets none of them, but a future
    one may). Asserted against the CONFIG values wherever a test needs the number, not a copy."""
    for k, v in {"NEXT_STEP_FIRST_DAYS": 2, "NEXT_STEP_AFTER_PREVIOUS_DAYS": 2, "NEXT_STEP_AFTER_EMAIL_DAYS": 7,
                 "NEXT_STEP_CALL_AFTER_DM_DAYS": 7, "NEXT_STEP_CALL_EVERY_DAYS": 3,
                 "NEXT_STEP_CALL_UNTIL_DAYS": 21, "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING": True,
                 "NEXT_STEP_TIME": "15:00"}.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "NEXT_STEP_CONNECTED_MARKERS", ["connected"])
    monkeypatch.setattr(config, "NEXT_STEP_DM_REPLIED_MARKERS", ["replied", "responded", "received"])
    monkeypatch.setattr(config, "NEXT_STEP_DM_SENT_MARKERS", ["sent", "received"])


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def run13(tab, today, state=None, **kw):
    """Rule 13 alone, through the real `nextaction.run`. `state` {} (nothing sent yet) unless given."""
    rule = rules_mod.by_id(R13)
    assert rule is not None, "R13 is not in bot_rules.yaml"
    return nextaction.run(today=today, rows=list(tab.rows), day_rules=[rule],
                          next_step_state={} if state is None else state, **kw)


def picked(res):
    """R13's items, in post order."""
    items = [a for a in res["actions"] if a.get("rule_id") == R13]
    return sorted(items, key=lambda a: a.get("pick_order", 0))


def names(res):
    return [a["poc"] for a in picked(res)]


def one(today, headers=None, **cells):
    """The R13 item for one made-up Connected person, or None. LI Connected Date defaults to 1 Sep 2026."""
    cells.setdefault("li_date", date(2026, 9, 1))
    tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs", **cells)], headers)
    items = picked(run13(tab, today))
    assert len(items) <= 1
    return items[0] if items else None


def first_due(start=date(2026, 9, 1), span=60, **cells):
    """The first day on or after `start` that the person is due, with its item."""
    for n in range(span):
        d = start + timedelta(days=n)
        it = one(d, **cells)
        if it is not None:
            return d, it
    return None, None


def step_of(item):
    return item.get("step"), item.get("ask")


def ask_of(item):
    return item.get("ask")


# ---------------------------------------------------------------------------------------------
# H. header mapping
# ---------------------------------------------------------------------------------------------

EXPECT_7OCT = {
    "sr_no": "A", "company": "B", "industry": "C", "name": "D", "designation": "E", "email": "F",
    "based": "G", "paper_links": "H", "li_url": "I", "first_contact": "J", "first_contact_type": "K",
    "first_contact_date": "L", "sid_li_added": "M", "li_connected_date": "N", "li_dm_sent": "O",
    "li_dm_date": "P", "outreach_step": "Q", "email_1_sent": "R", "email_1_date": "S",
    "email_2_sent": "T", "email_2_date": "U", "email_3_sent": "V", "email_3_date": "W",
    "meeting_date": "X", "meeting_status": "Y", "next_steps": "Z", "package": "AA",
    "prospect_status": "AB", "closure_prob": "AC", "deal_size": "AD", "deal_status": "AE",
    "poc_priority": "AF",
}
EXPECT_PRE = {
    "sr_no": "A", "company": "B", "industry": "C", "name": "D", "designation": "E", "email": "F",
    "based": "G", "paper_links": "H", "li_url": "I", "first_contact": "J", "first_contact_type": "K",
    "first_contact_date": "L", "sid_li_added": "M", "li_connected_date": "N", "li_dm_sent": "O",
    "li_dm_date": "P", "meeting_date": "Q", "meeting_status": "R", "next_steps": "S", "package": "T",
    "prospect_status": "U", "closure_prob": "V", "deal_size": "W", "deal_status": "X",
}
NEW_ROLES = ("outreach_step", "email_1_sent", "email_1_date", "email_2_sent", "email_2_date",
             "email_3_sent", "email_3_date", "poc_priority")


def role_letters(tab):
    m = tab.canonical_role_to_col or tab.role_to_col
    return {r: letter(i) for r, i in m.items()}


class TestHeaderMapping:
    def test_H1_todays_row_every_role_on_its_own_column(self, monkeypatch):
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {})
        tab = fx.parse([fx.values_row(1)])
        got = role_letters(tab)
        for role, col in EXPECT_7OCT.items():
            assert got.get(role) == col, f"{role}: want {col}, got {got.get(role)}"

    def test_H1_the_notes_role_is_Z_and_the_dropdown_is_Q(self, monkeypatch):
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {})
        tab = fx.parse([fx.values_row(1)])
        got = role_letters(tab)
        assert got["next_steps"] == "Z" and got["outreach_step"] == "Q"
        assert tab.headers[tab.role_to_col["next_steps"]] == "Notes/Remarks"
        assert tab.headers[tab.role_to_col["outreach_step"]] == "Next Steps"

    def test_H1_a_row_reads_each_cell_through_its_role(self):
        row = fx.parse([fx.person(1, step="Send email 1", notes="asked for a deck", e1="yes",
                                  e1d=date(2026, 9, 10), priority="P1")]).rows[0]
        assert row["outreach_step"] == "Send email 1" and row["next_steps"] == "asked for a deck"
        assert row["email_1_sent"] == "yes" and row["email_1_date"] == "10-09-2026"
        assert row["poc_priority"] == "P1"

    def test_H2_pre_7oct_row_still_maps_and_the_new_roles_are_absent(self, monkeypatch):
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {})
        tab = fx.parse([[""] * len(fx.HEADERS_PRE_7OCT)], fx.HEADERS_PRE_7OCT)
        got = role_letters(tab)
        for role, col in EXPECT_PRE.items():
            assert got.get(role) == col, f"{role}: want {col}, got {got.get(role)}"
        assert got["next_steps"] == "S"
        for role in NEW_ROLES:
            assert role not in got, f"{role} must not map on the pre-7 Oct row"

    def test_H3_veto_next_steps_header_never_becomes_the_notes_role(self, monkeypatch, caplog):
        """Even when GTM_COLUMN_MAP points the notes role at the dropdown (the old stop-gap run backwards)."""
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {"outreach_pocs": {"next_steps": "Next Steps"}})
        with caplog.at_level(logging.WARNING):
            tab = fx.parse([fx.values_row(1)])
        got = role_letters(tab)
        assert got["next_steps"] == "Z" and got["outreach_step"] == "Q"
        assert any("ignored" in r.getMessage() for r in caplog.records), "the vetoed override must be logged"

    def test_H3_a_notes_style_header_never_becomes_the_step_role(self, monkeypatch):
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {})
        hdr = [h if h != "Next Steps" else "Next Steps / Notes (new)" for h in fx.HEADERS_7OCT]
        hdr = [h if h != "Notes/Remarks" else "Comments" for h in hdr]
        tab = fx.parse([[""] * len(hdr)], hdr)
        got = role_letters(tab)
        assert got.get("outreach_step") != "Q", "a notes-style header must never become the step dropdown"

    def test_H3_the_step_role_override_cannot_point_at_a_notes_header(self, monkeypatch):
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {"outreach_pocs": {"outreach_step": "Notes/Remarks"}})
        got = role_letters(fx.parse([fx.values_row(1)]))
        assert got["outreach_step"] == "Q" and got["next_steps"] == "Z"

    def test_H3_blanking_the_stopgap_changes_nothing(self, monkeypatch):
        """The server's old stop-gap {"outreach_pocs":{"next_steps":"Notes/Remarks"}} is harmless and blank is the same."""
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {"outreach_pocs": {"next_steps": "Notes/Remarks"}})
        with_gap = role_letters(fx.parse([fx.values_row(1)]))
        monkeypatch.setattr(config, "GTM_COLUMN_MAP", {})
        assert role_letters(fx.parse([fx.values_row(1)])) == with_gap

    def test_the_new_roles_are_in_the_startup_schema_report(self):
        for role in NEW_ROLES:
            assert role in gtm_sheet.NEXT_ACTION_ROLES, role
        for role in NEW_ROLES:
            assert role not in gtm_sheet.CADENCE_ROLES, f"{role} would warn 'empty column' on every read"

    def test_H4_R9_follows_up_when_notes_are_blank_and_the_step_is_filled(self):
        """Today's row: Q holds the dropdown. R9 must read Z, so a filled Q with a blank Z is still 'no next steps'."""
        meeting = date(2026, 10, 5)
        tab = fx.parse([fx.person(1, name="Priya Rao", step="Send email 1", meeting_date=meeting,
                                  meeting_status="Completed")])
        res = nextaction.run(today=date(2026, 10, 12), rows=list(tab.rows), day_rules=[rules_mod.by_id("R9")])
        assert [a["poc"] for a in res["actions"]] == ["Priya Rao"]

    def test_H4_R9_is_silent_when_notes_have_text_even_with_the_step_blank(self):
        meeting = date(2026, 10, 5)
        tab = fx.parse([fx.person(1, name="Priya Rao", step="", notes="sending the MSA Friday",
                                  meeting_date=meeting, meeting_status="Completed")])
        res = nextaction.run(today=date(2026, 10, 12), rows=list(tab.rows), day_rules=[rules_mod.by_id("R9")])
        assert res["actions"] == []

    def test_H4_the_notes_role_reads_the_notes_column_not_the_dropdown(self):
        # (`nextaction.last_note` read this for R7's line until 13 Oct 2026; R7 shows no note now.)
        row = fx.parse([fx.person(1, step="Send email 1", notes="legal is reviewing")]).rows[0]
        assert nextaction._text(row, "next_steps") == "legal is reviewing"


# ---------------------------------------------------------------------------------------------
# B. bands
# ---------------------------------------------------------------------------------------------

class FakeWs:
    def __init__(self):
        self.updates = []

    def update(self, values=None, range_name="", value_input_option=""):
        self.updates.append((range_name, values[0][0]))


class FakeSh:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, title):
        return self.ws


@pytest.fixture
def sheet(monkeypatch, bands):
    """One 7 Oct tab behind a fake worksheet that records every cell write (and a refusal writes none)."""
    tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs", step="Research the PoC",
                              li_date=date(2026, 9, 1))])
    ws = FakeWs()
    monkeypatch.setattr(config, "SHEET_WRITES_ENABLED", True)
    monkeypatch.setattr(gtm_sheet.SHEETS, "read", lambda *a, **k: {gtm_sheet.POCS: tab})
    monkeypatch.setattr(gtm_sheet.SHEETS, "_open", lambda *a, **k: FakeSh(ws))
    monkeypatch.setattr(gtm_sheet.SHEETS, "_refuse_if_read_only", lambda *a, **k: "")
    return tab, ws


class TestBands:
    """THE WRITE LOCK IS BY ROLE (12 Oct 2026). These used to pin the letter bands A:I, Q:W, Z:AE; the roles
    they protect are the same and LI DM Sent, LI DM Date and Priority joined them. More in verify_sheet_locks.py."""

    def test_the_contract_defaults_in_env_example(self):
        text = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.example"),
                    encoding="utf-8").read()
        assert re.search(r"^RESTRICTED_COLUMN_ROLES=" + ",".join(IDENTITY + STEP + COMMERCIAL) + r"\s*$", text, re.M)
        assert re.search(r"^NEW_ROW_WRITABLE_ROLES=" + ",".join(NEW_ROW_DEFAULT) + r"\s*$", text, re.M)
        assert re.search(r"^RESTRICTED_COLUMN_RANGES=\s*$", text, re.M)
        assert re.search(r"^NEW_ROW_WRITABLE_RANGES=\s*$", text, re.M)

    def test_the_code_defaults_when_the_variables_are_empty(self):
        """A subprocess with the letter ranges blank gets the code defaults: no ranges, and the role lists."""
        import subprocess
        import sys
        env = dict(os.environ, RESTRICTED_COLUMN_RANGES="", NEW_ROW_WRITABLE_RANGES="", DISCORD_TOKEN="x",
                   ANTHROPIC_API_KEY="x")
        env.pop("RESTRICTED_COLUMN_ROLES", None)
        env.pop("NEW_ROW_WRITABLE_ROLES", None)
        out = subprocess.run(
            [sys.executable, "-c",
             "import config;print('|'.join([config.RESTRICTED_COLUMN_RANGES, config.NEW_ROW_WRITABLE_RANGES, "
             "str(len(config.RESTRICTED_COLUMN_ROLES)), str(len(config.NEW_ROW_WRITABLE_ROLES)), "
             "str(config.is_restricted_column(9))]))"],
            capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)), timeout=60)
        # ...and before any tab has been read, every column is restricted.
        assert out.stdout.strip().splitlines()[-1] == "||25|14|True", out.stdout + out.stderr

    def test_B1_the_window_is_J_to_N_and_X_to_Y(self, bands):
        assert config.writable_windows() == [(9, 13), (23, 24)]
        assert config.writable_window_label() == "J:N, X:Y"
        assert config.writable_roles() == ["first_contact", "first_contact_type", "first_contact_date",
                                           "sid_li_added", "li_connected_date", "meeting_date", "meeting_status"]

    def test_B1_the_gtm_window_log_line_names_the_writable_columns(self, bands, caplog):
        tab = fx.parse([fx.values_row(1)])
        with caplog.at_level(logging.INFO):
            gtm_sheet.SHEETS.log_writable_window(tab)
        line = next(r.getMessage() for r in caplog.records if "[gtm.window]" in r.getMessage())
        assert "restricted today at A:I, O:W, Z:AF" in line and "WRITABLE on an existing row: J:N, X:Y" in line
        for col, header in (("J", "First Contact"), ("N", "LI Connected Date"), ("X", "Meeting Date"),
                            ("Y", "Meeting Status")):
            assert f"{col}='{header}'" in line, (col, line)
        for col in ("O=", "P=", "Q=", "R=", "W=", "Z=", "AE=", "AF="):
            assert col not in line, f"{col} is a restricted column and must not be listed as writable"
        locks = [r.getMessage() for r in caplog.records if "[gtm.lock]" in r.getMessage()]
        assert any("outreach_step=Q 'Next Steps'" in m and "li_dm_sent=O 'LI DM Sent'" in m for m in locks)
        assert any("poc_priority=AF 'Priority'" in m for m in locks)

    @pytest.mark.parametrize("role,col", [("outreach_step", "Q"), ("email_1_sent", "R"), ("email_3_date", "W"),
                                          ("li_dm_sent", "O"), ("li_dm_date", "P"),
                                          ("next_steps", "Z"), ("deal_status", "AE"), ("poc_priority", "AF")])
    def test_B2_writes_to_the_step_and_commercial_roles_are_refused_on_an_existing_row(self, sheet, role, col):
        tab, ws = sheet
        out = gtm_sheet.SHEETS.write_cells(row=2, values={role: "x"}, expect_company="Acme Labs")
        assert out["ok"] is False and out["written"] == []
        assert [r["role"] for r in out["refused"]] == [role]
        assert out["refused"][0]["column"] == col
        assert ws.updates == [], "a refused role must reach the worksheet as no request at all"

    @pytest.mark.parametrize("role,col", [("first_contact", "J"), ("li_connected_date", "N"), ("meeting_date", "X"),
                                          ("meeting_status", "Y")])
    def test_B2_the_window_roles_are_still_writable(self, sheet, role, col):
        tab, ws = sheet
        out = gtm_sheet.SHEETS.write_cells(row=2, values={role: "yes"}, expect_company="Acme Labs")
        assert out["ok"] is True and ws.updates == [(f"{col}2", "yes")]

    def test_B2_one_refused_role_does_not_block_a_writable_one_but_never_reaches_the_sheet(self, sheet):
        tab, ws = sheet
        out = gtm_sheet.SHEETS.write_cells(row=2, values={"meeting_date": "21-10-2026", "outreach_step": "x"},
                                           expect_company="Acme Labs")
        assert [u[0] for u in ws.updates] == ["X2"]
        assert [r["role"] for r in out["refused"]] == ["outreach_step"]

    def test_B3_a_new_row_fills_only_the_identity_block_and_the_first_contact_cells(self, bands):
        for col in range(0, 14):                                    # A..N
            assert config.may_write_new_row_column(col), col
        for col in range(14, 32):                                   # O..AF: step, meeting, commercial, Priority
            assert not config.may_write_new_row_column(col), col

    def test_B3_append_row_refuses_the_step_notes_and_commercial_roles(self, sheet, monkeypatch):
        tab, ws = sheet
        monkeypatch.setattr(config, "SHEET_APPENDABLE_TABS", ["outreach_pocs"])
        grid = [list(fx.HEADERS_7OCT), [""] * 32]
        wsx = type("W", (), {"get_all_values": lambda s: grid, "row_values": lambda s, n: [],
                             "batch_update": lambda s, *a, **k: None})()
        monkeypatch.setattr(gtm_sheet.SHEETS, "_open", lambda *a, **k: FakeSh(wsx))
        out = gtm_sheet.SHEETS.append_row(
            tab, {"company": "Nova Labs", "name": "Someone New", "outreach_step": "Send email 1",
                  "email_1_sent": "yes", "next_steps": "hello", "closure_prob": "80%", "li_dm_sent": "Sent",
                  "poc_priority": "P1"},
            reason="test", dry_run=True)
        refused = {r["role"] for r in out.get("refused", [])}
        written = {c["role"] for c in out.get("written", [])}
        locked = {"outreach_step", "email_1_sent", "next_steps", "closure_prob", "li_dm_sent", "poc_priority"}
        assert locked <= refused
        assert "company" in written and "name" in written
        assert not (written & locked)

    def test_B4_write_cells_on_refuses_the_outreach_pocs_tab(self, sheet, monkeypatch):
        tab, ws = sheet
        monkeypatch.setattr(config, "SHEET_APPENDABLE_TABS", ["outreach_pocs"])
        for role in ("outreach_step", "email_1_sent", "next_steps", "first_contact"):
            out = gtm_sheet.SHEETS.write_cells_on(tab, row=2, values={role: "x"})
            assert not out.get("ok") and out.get("written") in ([], None), role
        assert ws.updates == []

    def test_B5_the_new_roles_are_never_allowed_by_the_tier(self):
        import sheetwrite
        for role in NEW_ROLES:
            for trig in (sheetwrite.TRIGGER_REPLY, getattr(sheetwrite, "TRIGGER_SWEEP", sheetwrite.TRIGGER_REPLY)):
                assert sheetwrite.allowed_for(role, trig) is False, (role, trig)

    def test_B5_plan_writes_turns_a_step_or_commercial_role_into_an_ask_never_a_write(self, bands):
        import sheetwrite
        tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs")])
        row = tab.rows[0]
        for role in ("outreach_step", "email_1_sent", "email_3_date", "li_dm_sent", "li_dm_date", "next_steps",
                     "deal_status", "poc_priority"):
            plan = sheetwrite.plan_writes(tab=tab, row=row, fields=[{"role": role, "value": "x", "supersedes": False,
                                                                     "quote": "x"}],
                                          trigger=sheetwrite.TRIGGER_REPLY, reply_text="x")
            assert plan["writes"] == {}, role

    def test_B6_a_new_row_setting_that_reaches_a_locked_block_is_named_and_not_opened(self, bands):
        bands(new_row_roles=NEW_ROW_DEFAULT + ["outreach_step", "deal_status"])
        assert config.new_row_locked_overlap() == "outreach_step, deal_status"
        assert not config.may_write_new_row_column(16) and not config.may_write_new_row_column(30)
        bands(new_row_ranges="A:R")                       # the laptop's old letter band
        assert config.new_row_locked_overlap() == "O:R"
        assert [config.may_write_new_row_column(c) for c in (13, 14, 15, 16, 17)] == [True, False, False, False,
                                                                                    False]

    def test_B6_no_warning_for_the_contract_lists(self, bands):
        bands()
        assert config.new_row_locked_overlap() == ""

    def test_a_restricted_role_with_no_column_refuses_every_write(self, sheet, bands, caplog, monkeypatch):
        _tab, ws = sheet
        headers = [h if h != "Next Steps" else "Column the bot cannot name" for h in fx.HEADERS_7OCT]
        getattr(gtm_sheet.SHEETS, "_lock_failures_logged", set()).clear()    # it is logged once per failure
        with caplog.at_level(logging.ERROR):
            broken = bands(headers=headers)
        monkeypatch.setattr(gtm_sheet.SHEETS, "read", lambda *a, **k: {gtm_sheet.POCS: broken})
        assert broken.lock.unmapped == ("outreach_step",)
        assert any("outreach_step" in r.getMessage() and "REFUSED" in r.getMessage() for r in caplog.records)
        out = gtm_sheet.SHEETS.write_cells(row=2, values={"meeting_date": "21-10-2026"}, expect_company="Acme Labs 01")
        assert out["ok"] is False and ws.updates == []

    def test_a_letter_range_can_only_lock_more(self, bands):
        bands(restricted_ranges="X:Y")
        assert config.is_restricted_column(23) and config.is_restricted_column(24)       # now locked too
        assert config.is_restricted_column(16) and not config.is_restricted_column(9)    # roles unchanged
        bands(restricted_roles=IDENTITY + STEP + COMMERCIAL, restricted_ranges="")
        assert not config.is_restricted_column(23)

    def test_email_write_still_writes_only_the_email_cell(self, monkeypatch, bands):
        tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs")])
        ws = FakeWs()
        monkeypatch.setattr(config, "EMAIL_WRITE_ALLOWED", True)
        monkeypatch.setattr(config, "SHEET_WRITES_ENABLED", True)
        monkeypatch.setattr(gtm_sheet.SHEETS, "read", lambda *a, **k: {gtm_sheet.POCS: tab})
        monkeypatch.setattr(gtm_sheet.SHEETS, "_open", lambda *a, **k: FakeSh(ws))
        monkeypatch.setattr(gtm_sheet.SHEETS, "_refuse_if_read_only", lambda *a, **k: "")
        out = gtm_sheet.SHEETS.write_email(row=2, email="priya@acme.example", expect_company="Acme Labs",
                                           expect_name="Priya Rao")
        assert out["ok"] is True and [w for w in ws.updates] == [("F2", "priya@acme.example")]


# ---------------------------------------------------------------------------------------------
# state helpers (the sender's job, done by hand: record only what a real send would)
# ---------------------------------------------------------------------------------------------

def send(db, res, day):
    """What `_after_send` does for a real send: record each named person. Returns the items."""
    items = picked(res)
    for it in items:
        db.record_next_step_mention(it["row_key"], signature=it["signature"], on_date=dl.iso(day), ask=it["ask"])
    return items


def tables(db):
    with db.conn() as c:
        return ([tuple(r) for r in c.execute("SELECT * FROM next_step_followups ORDER BY row_key").fetchall()],
                [tuple(r) for r in c.execute("SELECT * FROM next_step_posts ORDER BY message_id").fetchall()])


C0 = date(2026, 9, 1)
D = timedelta


# ---------------------------------------------------------------------------------------------
# step_code and who is in
# ---------------------------------------------------------------------------------------------

class TestStepCode:
    @pytest.mark.parametrize("raw,code", [
        ("", "blank"), ("   ", "blank"), ("Research the PoC", "research"), ("research poc", "research"),
        ("RESEARCH THE POC", "research"), ("Send email 1", "email1"), ("Send Email 1", "email1"),
        ("send email 2", "email2"), ("Send  email  3", "email3"), ("Reach by LI DM", "dm"),
        ("reach by linkedin dm", "dm"), ("Call the PoC", "call"), ("Call PoC", "call"), ("call the poc", "call"),
        ("Send email 4", "unknown"), ("Follow up later", "unknown"), ("Sent email 1", "unknown"),
    ])
    def test_normalised_values(self, raw, code):
        assert nextaction.step_code(raw) == code


class TestWhoIsIn:
    def test_only_connected_rows_normalised_exactly(self):
        inc = ["Connected", "connected", " CONNECTED ", "Connected "]
        out = ["Not connected", "Connection sent", "Requested", "", "Disconnected", "connected?"]
        rows = [fx.person(i + 1, name=f"In {i}", connected=v, li_date=C0, step="Research the PoC")
                for i, v in enumerate(inc + out)]
        res = run13(fx.parse(rows), date(2026, 10, 12))
        assert sorted(names(res)) == sorted(f"In {i}" for i in range(len(inc)))

    def test_a_snoozed_row_is_out(self):
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=C0, step="Research the PoC")])
        first = picked(run13(tab, date(2026, 10, 12)))[0]
        res = run13(tab, date(2026, 10, 12), snoozes={first["row_key"]: {"until_date": "2026-10-20"}})
        assert picked(res) == []

    def test_priority_never_changes_the_order(self):
        tab = fx.parse([fx.person(1, name="First P2", li_date=C0, step="Research the PoC", priority="P2"),
                        fx.person(2, name="Second P1", li_date=C0, step="Research the PoC", priority="P1")])
        assert names(run13(tab, date(2026, 10, 12))) == ["First P2", "Second P1"]


# ---------------------------------------------------------------------------------------------
# T. the timing table, one row at a time. The first day a person is due is found by scanning from 1 Sep,
# so the due date, not the implementation's own arithmetic, is what is asserted.
# ---------------------------------------------------------------------------------------------

class TestTimingTable:
    def test_T3_applies_even_when_the_date_cell_holds_a_date_but_sent_is_blank(self):
        d, it = first_due(step="Send email 1", e1d=date(2026, 9, 20))
        assert d == C0 + D(days=2) and ask_of(it) == "email_out"

    def test_T4_the_date_can_be_typed_as_DD_dot_MM_dot_YYYY(self):
        d, it = first_due(step="Send email 1", e1="yes", e1d="10.09.2026")
        assert d == date(2026, 9, 17) and ask_of(it) == "advance"

    def test_T11_a_person_first_seen_after_the_window_goes_straight_to_the_unresponsive_reminder(self):
        it = one(date(2026, 10, 12), step="Call the PoC", dm_sent="Sent", dm_date=date(2026, 8, 1))
        assert ask_of(it) == "unresponsive"

    @pytest.mark.parametrize("variants,canon", [
        (("Send Email 1", "send email 1", "SEND EMAIL 1", "Send email  1 "), "Send email 1"),
        (("Call PoC", "call the poc", "Call The PoC"), "Call the PoC"),
        (("Research PoC", "research the poc"), "Research the PoC"),
        (("Reach By LI DM", "reach by linkedin dm"), "Reach by LI DM"),
    ])
    def test_normalised_q_values_behave_identically(self, variants, canon):
        extra = dict(dm_sent="Sent", dm_date=date(2026, 9, 14)) if canon in ("Call the PoC", "Reach by LI DM") else {}
        want = first_due(step=canon, **extra)
        assert want[0] is not None
        for v in variants:
            got = first_due(step=v, **extra)
            assert (got[0], ask_of(got[1])) == (want[0], ask_of(want[1])), v

    def test_an_unknown_q_value_is_skipped_logged_and_listed(self, caplog):
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=C0, step="Circle back in Q4")])
        with caplog.at_level(logging.INFO):
            res = run13(tab, date(2026, 10, 12))
        assert picked(res) == []
        rep = res["reports"][R13]
        assert [e["poc"] for e in rep["unknown_step"]] == ["Priya Rao"]
        assert "Circle back in Q4" in str(rep["unknown_step"][0])
        assert "Circle back in Q4" in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------------------
# C. the call clock, the Unresponsive reminder, defaults f and g
# ---------------------------------------------------------------------------------------------

def weekdays_from(start, n):
    return [d for d in (start + D(days=i) for i in range(n)) if d.weekday() < 5]


DM_DAY = date(2026, 9, 21)         # a Monday


def dm_person(**cells):
    cells.setdefault("step", "Reach by LI DM")
    return fx.person(1, name="Priya Rao", company="Acme Labs", li_date=date(2026, 8, 1), dm_sent="Sent",
                     dm_date=DM_DAY, **cells)


class TestCallClock:
    def walk(self, db, tab, first, last, change=None):
        out = []
        for d in weekdays_from(first, (last - first).days + 1):
            if change and d in change:
                tab = fx.parse([change[d]])
            res = run13(tab, d, state=db.next_step_state())
            for it in send(db, res, d):
                out.append((d, it["ask"]))
        return out

    def test_C1_calls_on_days_7_10_14_17_21_then_unresponsive_on_day_22_then_never(self, db):
        tab = fx.parse([dm_person()])
        got = self.walk(db, tab, DM_DAY, date(2026, 10, 30))
        calls = [DM_DAY + D(days=n) for n in (7, 10, 14, 17, 21)]
        assert got[:5] == [(d, "call") for d in calls], got
        # CHANGED 12 Oct 2026: the unresponsive reminder is no longer a one-off. Nobody is dropped, so from day
        # 22 it comes round on every post until Prospect Status says Unresponsive (it was "then never").
        rest = got[5:]
        assert rest[0] == (DM_DAY + D(days=22), "unresponsive") and len(rest) > 1, rest
        assert {a for _d, a in rest} == {"unresponsive"}

    def test_C1_a_missed_post_does_not_lose_the_unresponsive_reminder(self, db):
        tab = fx.parse([dm_person()])
        got = self.walk(db, tab, DM_DAY + D(days=28), DM_DAY + D(days=40))          # Mon 19 Oct, 28 days after the DM
        assert got[0] == (DM_DAY + D(days=28), "unresponsive") and {a for _d, a in got} == {"unresponsive"}

    def test_C1_the_numbers_come_from_config(self, db, monkeypatch):
        monkeypatch.setattr(config, "NEXT_STEP_CALL_AFTER_DM_DAYS", 4)
        monkeypatch.setattr(config, "NEXT_STEP_CALL_EVERY_DAYS", 4)
        monkeypatch.setattr(config, "NEXT_STEP_CALL_UNTIL_DAYS", 12)
        got = self.walk(db, fx.parse([dm_person()]), DM_DAY, DM_DAY + D(days=30))
        # Fri 25 Sep (day 4), Tue 29 Sep (day 8), then day 12 is a Saturday: Monday 5 Oct is past it, so the
        # Unresponsive reminder, not a third call
        assert got[:3] == [(DM_DAY + D(days=4), "call"), (DM_DAY + D(days=8), "call"),
                           (DM_DAY + D(days=14), "unresponsive")], got
        assert {a for _d, a in got[3:]} <= {"unresponsive"}          # it repeats; nothing closes a person

    def test_C2_changing_q_from_dm_to_call_mid_clock_adds_no_reminder_and_the_count_continues(self, db):
        got = self.walk(db, fx.parse([dm_person(step="Reach by LI DM")]), DM_DAY, DM_DAY + D(days=26),
                        change={DM_DAY + D(days=8): dm_person(step="Call the PoC")})
        assert [d for d, a in got if a == "call"] == [DM_DAY + D(days=n) for n in (7, 10, 14, 17, 21)]
        state = db.next_step_state()
        assert state[next(iter(state))]["calls"] == 5

    def test_C3_unresponsive_in_prospect_status_stops_it_at_every_point(self):
        for day_n in (0, 3, 7, 10, 22, 40):
            t = fx.parse([dm_person(prospect="Unresponsive")])
            assert picked(run13(t, DM_DAY + D(days=day_n))) == [], day_n

    def test_C5_default_g_a_meeting_booked_for_tomorrow_pauses_and_is_listed(self):
        today = date(2026, 10, 12)
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=C0, step="Research the PoC",
                                  meeting_date=today + D(days=1))])
        res = run13(tab, today)
        assert picked(res) == []
        assert [e["poc"] for e in res["reports"][R13]["paused"]] == ["Priya Rao"]

    def test_C5_a_meeting_dated_today_also_pauses(self):
        today = date(2026, 10, 12)
        assert one(today, step="Research the PoC", meeting_date=today) is None

    def test_C5_a_past_meeting_not_marked_completed_lets_the_call_step_run(self):
        today = date(2026, 10, 12)
        it = one(today, step="Call the PoC", dm_sent="Sent", dm_date=date(2026, 9, 21),
                 meeting_date=today - D(days=7), meeting_status="Scheduled")
        assert it is not None and ask_of(it) == "call"

    def test_C5_a_completed_meeting_ends_rule_13_for_them(self):
        today = date(2026, 10, 12)
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=C0, step="Research the PoC",
                                  meeting_date=today - D(days=7), meeting_status="Completed")])
        res = run13(tab, today)
        assert picked(res) == []
        done = res["reports"][R13]["completed"]
        assert (done if isinstance(done, int) else len(done)) == 1

    def test_C5_with_the_switch_off_a_future_meeting_does_not_pause(self, monkeypatch):
        monkeypatch.setattr(config, "NEXT_STEP_PAUSE_FOR_BOOKED_MEETING", False)
        today = date(2026, 10, 12)
        assert one(today, step="Research the PoC", meeting_date=today + D(days=1)) is not None


# ---------------------------------------------------------------------------------------------
# R. the rotation: 33 Connected rows (29 dated) over 10 weekdays
# ---------------------------------------------------------------------------------------------

def rotation_runs(db, rows, days=None, edit=None):
    """Run Rule 13 on each day, recording what the sender would. `edit(i, day, rows)` may change the sheet."""
    out = []
    for i, d in enumerate(days or fx.WEEKDAYS):
        if edit:
            edit(i, d, rows)
        res = run13(fx.parse(rows), d, state=db.next_step_state())
        out.append((d, res, send(db, res, d)))
    return out


def poc_names(items):
    return [it["poc"] for it in items]


class TestRotation:
    def test_R1_a_person_is_named_every_day_while_fewer_than_five_are_due(self, db):
        tab = fx.parse([fx.person(i, name=f"Only {i}", li_date=C0, step="Research the PoC") for i in (1, 2, 3)])
        for d in fx.WEEKDAYS[:4]:
            res = run13(tab, d, state=db.next_step_state())
            assert poc_names(send(db, res, d)) == ["Only 1", "Only 2", "Only 3"]

    def test_R4_nobody_due_means_no_item_no_post_and_no_state_row(self, db):
        tab = fx.parse([fx.person(i, name=f"Fresh {i}", li_date=fx.FIRST_DAY - D(days=1), step="Research the PoC")
                        for i in (1, 2, 3)])
        res = run13(tab, fx.FIRST_DAY, state=db.next_step_state())
        assert picked(res) == [] and not [a for a in res["actions"] if a.get("rule_id") == R13]
        assert tables(db) == ([], [])
        assert res["reports"][R13]["waiting"] in (3, [1, 2, 3]) or res["reports"][R13]["waiting"]

    def test_R5_unreadable_state_posts_nothing_and_says_so(self):
        rows, _ = fx.rotation_values()
        res = nextaction.run(today=fx.FIRST_DAY, rows=list(fx.parse(rows).rows),
                             day_rules=[rules_mod.by_id(R13)], next_step_state=None)
        assert picked(res) == []
        assert res["reports"][R13]["state"] == "unreadable"

    def test_the_evaluator_is_pure_it_writes_no_state(self, db):
        rows, _ = fx.rotation_values()
        tab = fx.parse(rows)
        for d in fx.WEEKDAYS[:3]:
            run13(tab, d, state=db.next_step_state())
            run13(tab, d, state=db.next_step_state())
        assert tables(db) == ([], [])


# ---------------------------------------------------------------------------------------------
# the state table
# ---------------------------------------------------------------------------------------------

class TestState:
    def test_a_fresh_database_has_both_tables_and_an_empty_state(self, db):
        assert db.next_step_state() == {}
        assert tables(db) == ([], [])

    def test_recording_a_mention_sets_the_signature_date_and_counts(self, db):
        db.record_next_step_mention("k1", signature="research|2026-09-01", on_date="2026-10-12", ask="researched")
        db.record_next_step_mention("k1", signature="research|2026-09-01", on_date="2026-10-14", ask="researched")
        e = db.next_step_state()["k1"]
        assert (e["signature"], e["last_date"], e["mentions"], e["calls"]) == (
            "research|2026-09-01", "2026-10-14", 2, 0)
        assert "closed" not in e and "first_named" not in e      # retained columns, no longer read

    def test_unreadable_state_fails_closed_to_none_not_an_empty_dict(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db, "conn", boom)
        assert db.next_step_state() is None

    def test_neither_table_is_kept_on_reset(self):
        import db as dbmod
        kept = " ".join(map(str, getattr(dbmod, "KEPT_ON_RESET", ())))
        assert "next_step_followups" not in kept and "next_step_posts" not in kept


# ---------------------------------------------------------------------------------------------
# D1, O1 — the shipped bot_rules.yaml
# ---------------------------------------------------------------------------------------------

TUESDAY, MONDAY, THURSDAY = date(2026, 10, 13), date(2026, 10, 12), date(2026, 10, 15)
SATURDAY, SUNDAY = date(2026, 10, 17), date(2026, 10, 18)


def ids(day):
    return [r.id for r in rules_mod.for_day(day)]


class TestShippedRules:
    def test_O1_thirteen_rules_in_file_order_with_R13_before_R5(self):
        loaded = rules_mod.safe_load()
        # R14 (NFT2-1086, 13 Oct) is a fourteenth rule, LAST in the file: it reads who R13 named today.
        assert [r.id for r in loaded] == ["R1", "R2", "R3", "R4", "R13", "R5", "R6", "R7", "R8", "R9", "R10",
                                          "R11", "R12", "R14"]

    def test_O1_the_R13_entry(self):
        r = rules_mod.by_id(R13)
        assert r.name == "Next steps for connected contacts"
        assert r.trigger == TRIGGER and r.max_items_per_post == 5 and r.destination == "channel"
        assert r.counts_toward_cap is False and r.enabled is True
        assert tuple(r.weekdays) == (0, 1, 2, 3, 4)
        assert r.plain and r.sheet_wording

    def test_O1_the_days(self):
        # SINCE NFT2-1069 (8 Oct) R11 runs on Wednesdays only. R7 IS OFF SINCE 16 OCT (RULES-OCT16).
        # R14 is anchored (no weekdays), like R8 and R9, so it gets to look every day.
        assert ids(MONDAY) == ["R1", "R4", "R13", "R8", "R9", "R10", "R14"]
        assert ids(THURSDAY) == ["R1", "R13", "R5", "R8", "R9", "R12", "R14"]
        assert ids(date(2026, 10, 14)) == ["R1", "R3", "R13", "R8", "R9", "R11", "R14"]
        assert ids(SATURDAY) == ["R8", "R9", "R14"]
        assert R13 not in ids(SATURDAY) and R13 not in ids(SUNDAY)
        for d in (MONDAY, TUESDAY, date(2026, 10, 14), THURSDAY, date(2026, 10, 16)):
            assert R13 in ids(d)

    def test_O1_R7_is_back_on_mondays_and_produces_nothing_for_a_person_rule_13_covers(self):
        """7 Oct: "rule 13 supercedes this", and R7 was switched off. 8 Oct (NFT2-1069): R7 is back on Mondays, but
        never for a person Rule 13 covers. The same person as before — Connected, with an LI Connected Date, DM'd
        20 days ago, no meeting — still gets nothing from R7, and is Rule 13's.

        16 Oct (RULES-OCT16): R7 is off; this runs with it TURNED BACK ON, to show it comes back as it was."""
        import dm_no_meeting_checks as dmc

        assert rules_mod.by_id("R7").enabled is False and "R7" not in ids(MONDAY)
        with dmc.r7_back_on():
            assert rules_mod.by_id("R7").enabled is True
            assert "R7" in ids(MONDAY) and "R7" not in ids(TUESDAY)
            tab = fx.parse([fx.person(1, name="Priya Rao", li_date=date(2026, 8, 1), dm_sent="Sent",
                                      dm_date=MONDAY - D(days=20), step="Call the PoC")])
            res = nextaction.run(today=MONDAY, rows=list(tab.rows), day_rules=rules_mod.for_day(MONDAY),
                                 next_step_state={}, dm_no_meeting_state={})
        # CHANGED 13 Oct 2026: R7 is the escalation behind R13 and no longer reads `covered_by_next_steps`. It
        # produces nothing here because R13, listed first, names the person today and R7 skips them.
        assert not [a for a in res["actions"] if a["rule_id"] == "R7"]
        assert not res["by_rule"].get("R7")
        assert [a["poc"] for a in res["by_rule"].get("R13") or []] == ["Priya Rao"]
        assert nextaction.covered_by_next_steps(tab.rows[0]) is True

    def test_O1_R7_lists_a_dm_d_person_rule_13_does_not_cover(self, monkeypatch):
        """The other half: the same row with "Sid - LI Addition" not saying Connected is nobody's but R7's.
        (13 Oct 2026: three weeks after the DM, not one; read off LI DM Sent, not the date.)"""
        monkeypatch.setattr(config, "DM_NO_MEETING_DAYS", 21)
        monkeypatch.setattr(config, "DM_NO_MEETING_DM_SENT_MARKERS", ["sent"])
        monkeypatch.setattr(config, "DM_NO_MEETING_DM_REPLIED_MARKERS", ["replied"])
        monkeypatch.setattr(config, "DM_NO_MEETING_STATUS_OPEN", ["", "tbd"])
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=date(2026, 8, 1), dm_sent="Sent",
                                  dm_date=MONDAY - D(days=21), step="Call the PoC")])
        row = dict(tab.rows[0], sid_li_added="")
        assert nextaction.covered_by_next_steps(row) is False
        import dm_no_meeting_checks as dmc

        with dmc.r7_back_on():                  # R7 is off since 16 Oct (RULES-OCT16)
            res = nextaction.run(today=MONDAY, rows=[row], day_rules=rules_mod.for_day(MONDAY),
                                 next_step_state={}, dm_no_meeting_state={})
        assert [a["poc"] for a in res["by_rule"].get("R7") or []] == ["Priya Rao"]
        assert not res["by_rule"].get("R13")

    def test_O1_rule_7_no_longer_reads_rule_13s_check(self):
        """Until 13 Oct 2026 R7 called `covered_by_next_steps` and left those people to R13; since 12 Oct that was
        every DM'd row, so R7 named nobody. It is the escalation behind R13 now, and only the dedup separates them."""
        import inspect
        r13 = inspect.getsource(nextaction._r_next_step_followups)
        r7 = inspect.getsource(nextaction._r_dm_no_meeting) + inspect.getsource(nextaction.dm_no_meeting_state)
        assert "marked_connected(" in r13 and "has_connected_date(" in r13
        assert "covered_by_next_steps(" not in r7
        assert "NEXT_STEP_CONNECTED_MARKERS" not in r13 and "NEXT_STEP_CONNECTED_MARKERS" not in r7
        assert "sid_li_added" not in r7 and "li_connected_date" not in r7

    def test_O1_the_R7_evaluator_is_kept(self):
        assert hasattr(nextaction, "_r_dm_no_meeting") and nextaction.R_DM_NO_MEETING in nextaction.EVALUATORS

    def test_O1_R6_no_longer_says_no_dm_logged(self):
        tab = fx.parse([fx.person(1, name="Priya Rao", li_date=TUESDAY - D(days=10), step="Send email 1",
                                  e1="", email="")])
        rule = rules_mod.by_id("R6")
        res = nextaction.run(today=TUESDAY, rows=list(tab.rows), day_rules=[rule], next_step_state={})
        items = [a for a in res["by_rule"]["R6"]]
        assert items, "R6 must still select a connected contact with no DM"
        # CHANGED 12 Oct 2026: R6's item line is the name and "connected, no DM yet"; how long ago they
        # connected is carried as a field (the post is four blocks rendered in code and prints no day count).
        for it in items:
            assert "no dm logged" not in it["text"].lower()
            assert "connected, no dm yet" in it["text"].lower() and it["connected_days"] == 10

    def test_O1_R6_keeps_its_trigger_and_days(self):
        r = rules_mod.by_id("R6")
        assert r.trigger == "li_no_dm" and r.enabled is True

    def test_O1_the_daily_cap_text_says_next_step_followups_dont_count(self):
        assert "next-step follow-ups" in rules_mod.global_rule("daily_cap")

    def test_D1_a_contact_both_R6_and_R13_select_is_kept_by_R13_and_R6_is_deduped(self):
        tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs", li_date=TUESDAY - D(days=10),
                                  step="Research the PoC")])
        day_rules = [r for r in rules_mod.for_day(TUESDAY) if r.id in ("R6", "R13")]
        assert [r.id for r in day_rules] == ["R13", "R6"]
        res = nextaction.run(today=TUESDAY, rows=list(tab.rows), day_rules=day_rules, next_step_state={})
        assert [a["rule_id"] for a in res["actions"]] == ["R13"]
        assert [d.get("rule_id") for d in res["deduped"]] == ["R6"] or any(
            "R6" in str(d) for d in res["deduped"])

    def test_the_rule_validates_at_startup_no_unknown_trigger(self):
        import rules
        assert TRIGGER in rules.KNOWN_TRIGGERS and TRIGGER in rules.PLAIN_BY_TRIGGER

    def test_R9_wording_says_notes_remarks(self):
        assert "Notes/Remarks" in rules_mod.by_id("R9").sheet_wording
        assert "Notes/Remarks" in rules_mod.sheet_wording_for("R9")

    def test_R7_sheet_wording_says_rule_13_comes_first(self):
        """It was the one-line "Replaced by rule 13" sentence until 8 Oct, when R7 came back on Mondays."""
        text = rules_mod.sheet_wording_for("R7")
        assert "Rule 13 comes first" in text and "not listed here" in text
        assert "Every Monday" in text and "replaced" not in text.lower()


# ---------------------------------------------------------------------------------------------
# P. timing: through drip.plan
# ---------------------------------------------------------------------------------------------

def spaced_item(i):
    return {"rule": "x", "rule_id": f"R{20 + i}", "type": f"spaced{i}", "rule_name": "x", "label": "x",
            "owner": f"Owner{i}", "priority": 2, "priority_label": "p", "due_date": TUESDAY,
            "due_iso": "2026-10-13", "overdue_days": 0, "company": f"Co{i}", "poc": "", "poc_designation": "",
            "sheet_row": 2, "row_key": f"co{i}|", "contact_key": f"co{i}|", "max_items_per_post": 5,
            "counts_toward_cap": True, "destination": "channel", "web_pending": False, "why": "x", "text": "x",
            "key": f"R{20 + i}:co{i}"}


def r13_actions(day):
    rows, _ = fx.rotation_values()
    return picked(run13(fx.parse(rows), day))


class TestPlan:
    def test_P1_one_pinned_15_00_post_outside_the_cap(self):
        import drip
        acts = r13_actions(THURSDAY)
        planned = drip.plan(acts, day=THURSDAY)
        mine = [m for m in planned["messages"] if m["type"] == TRIGGER]
        assert len(mine) == 1, "one post for the whole rule, not one per person"
        m = mine[0]
        assert m["pinned"] is True and m["send_at_hhmm"] == config.NEXT_STEP_TIME == "15:00"
        assert m["counts_toward_cap"] is False and planned["counted"] == 0
        assert len(m["actions"]) == 5 and m["max_items_per_post"] == 5

    def test_P1_is_never_held_two_days_running(self):
        import drip
        acts = r13_actions(THURSDAY)
        key = drip.group_key(acts[0])
        history = {key: {"last_nudge": dl.iso(THURSDAY - D(days=1))}}
        planned = drip.plan(acts, day=THURSDAY, history=history)
        mine = [m for m in planned["messages"] if m["type"] == TRIGGER]
        assert len(mine) == 1 and mine[0]["stage"] == drip.STAGE_NUDGE
        assert not [h for h in planned["held"] if h["type"] == TRIGGER]

    def test_P1_a_cap_of_zero_does_not_roll_it(self):
        import drip
        planned = drip.plan(r13_actions(THURSDAY), day=THURSDAY, cap=0)
        assert [m["type"] for m in planned["messages"]] == [TRIGGER]
        assert not [r for r in planned["rolled"] if r["type"] == TRIGGER]

    def test_P1_the_constants(self):
        import drip
        assert TRIGGER in drip.NEVER_HELD and TRIGGER in drip.RULE_ONLY_TYPES
        assert TRIGGER not in drip.NEVER_COUNTED, "default b lives in bot_rules.yaml, not in code"

    def test_P1_it_does_not_move_the_spaced_posts(self):
        import drip
        spaced = [spaced_item(i) for i in range(3)]
        alone = drip.plan(spaced, day=THURSDAY)
        both = drip.plan(spaced + r13_actions(THURSDAY), day=THURSDAY)
        times = lambda p: [(m["type"], m["send_at_hhmm"]) for m in p["messages"] if m["type"] != TRIGGER]  # noqa: E731
        assert times(alone) == times(both)

    def test_P1_the_post_is_not_addressed_to_the_deliverables_default_owner(self, monkeypatch):
        import drip
        monkeypatch.setattr(config, "DELIVERABLE_DEFAULT_OWNER", "Zed Owner")
        planned = drip.plan(r13_actions(THURSDAY), day=THURSDAY)
        m = next(m for m in planned["messages"] if m["type"] == TRIGGER)
        assert not m["owner"] and "Zed" not in str(m.get("owner_key") or "")

    def test_P1_the_time_is_a_setting(self, monkeypatch):
        import drip
        monkeypatch.setattr(config, "NEXT_STEP_TIME", "11:30")
        planned = drip.plan(r13_actions(THURSDAY), day=THURSDAY)
        assert next(m for m in planned["messages"] if m["type"] == TRIGGER)["send_at_hhmm"] == "11:30"

    def test_P1_the_live_gate_opens_for_a_time_before_the_window(self, monkeypatch):
        import drip
        monkeypatch.setattr(config, "NEXT_STEP_TIME", "07:45")
        assert drip.earliest_send_ist() <= (7, 45)

    def test_P2_saturday_and_sunday_are_silent(self):
        import drip
        acts = r13_actions(THURSDAY)
        for day in (SATURDAY, SUNDAY):
            planned = drip.plan(acts, day=day)
            assert not [m for m in planned["messages"] if m["type"] == TRIGGER], day

    def test_P2_every_weekday_posts(self):
        import drip
        for day in fx.WEEKDAYS:
            planned = drip.plan(r13_actions(day), day=day)
            assert len([m for m in planned["messages"] if m["type"] == TRIGGER]) == 1, day


# ---------------------------------------------------------------------------------------------
# V. the words
# ---------------------------------------------------------------------------------------------

SCHEDULE = re.compile(r"\b(every|daily|weekly|3 ?pm|days?)\b", re.I)
RULE_NUMBER = re.compile(r"\bR\d{1,2}\b|\brule \d+", re.I)


def all_r13_lines():
    import wording
    who = "Priya Rao (Acme Labs)"
    lines = []
    for ask in ("ask_next", "researched", "email_out", "advance", "log_date", "dm_out", "call",
                "call_no_dm_date", "dm_replied", "unresponsive"):
        for n in (1, 2, 3):
            for missing in ((), ("1st Email Date",), ("1st Email Sent", "1st Email Date")):
                for set_call in (False, True):
                    lines.append(wording.next_step_line(ask, who=who, step_label="Send email 2", n=n,
                                                        when="5 Oct", missing=missing, set_call=set_call))
    lines.append(wording.next_step_line("researched", who="Acme Labs", n=0))
    return sorted({ln for ln in lines if ln})


class TestWords:
    def test_V1_there_is_no_opener_line_any_more(self):
        """The three openers went on 8 Oct (REPLIES-OCT8): each group of the post opens on its own ask, so a line
        saying "here are next steps" under a heading that says it was clutter."""
        import wording
        assert not hasattr(wording, "NEXT_STEP_OPENERS")

    def test_V1_every_line_the_evaluator_makes_is_clean(self, db):
        import wording
        rows, _ = fx.rotation_values()
        for it in picked(run13(fx.parse(rows), fx.FIRST_DAY)):
            assert wording.register_problems(it["text"]) == [], it["text"]
            assert not RULE_NUMBER.search(it["text"]) and not SCHEDULE.search(it["text"]), it["text"]


# ---------------------------------------------------------------------------------------------
# startup validation (the warning that used to hard-code "S:X"), and settings
# ---------------------------------------------------------------------------------------------

class TestStartupValidation:
    def run_validate(self, caplog):
        with caplog.at_level(logging.INFO):
            config.validate()
        return [r for r in caplog.records]

    def test_B6_a_new_row_setting_reaching_into_a_locked_block_warns_and_names_it(self, monkeypatch, bands, caplog):
        monkeypatch.setattr(config, "SHEET_ROW_ADDITIONS_ENABLED", True)
        bands(new_row_roles=NEW_ROW_DEFAULT + ["outreach_step"])
        recs = self.run_validate(caplog)
        warn = [r.getMessage() for r in recs if r.levelno >= logging.WARNING and "NEW_ROW_WRITABLE_ROLES" in r.getMessage()]
        assert len(warn) == 1, warn
        assert "outreach_step" in warn[0] and "NOT opened" in warn[0]

    def test_B6_the_contract_lists_do_not_warn(self, monkeypatch, bands, caplog):
        monkeypatch.setattr(config, "SHEET_ROW_ADDITIONS_ENABLED", True)
        bands()
        recs = self.run_validate(caplog)
        assert not [r for r in recs if r.levelno >= logging.WARNING and "NEW_ROW_WRITABLE_ROLES" in r.getMessage()]

    def test_B6_an_old_letter_band_for_new_rows_warns_with_the_letters_it_cannot_open(self, monkeypatch, bands, caplog):
        """The laptop's .env still says NEW_ROW_WRITABLE_RANGES=A:R: on the 7 Oct order that reaches O to R."""
        monkeypatch.setattr(config, "SHEET_ROW_ADDITIONS_ENABLED", True)
        bands(new_row_ranges="A:R")
        warn = [r.getMessage() for r in self.run_validate(caplog) if "NEW_ROW_WRITABLE_ROLES" in r.getMessage()
                and r.levelno >= logging.WARNING]
        assert len(warn) == 1 and "O:R" in warn[0], warn

    def test_a_next_step_time_that_is_not_hh_mm_falls_back_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert config._hhmm("NEXT_STEP_TIME", "25:99", "15:00") == "15:00"
            assert config._hhmm("NEXT_STEP_TIME", "3pm", "15:00") == "15:00"
            assert config._hhmm("NEXT_STEP_TIME", "9:05", "15:00") == "09:05"
        assert len([r for r in caplog.records if "NEXT_STEP_TIME" in r.getMessage()]) == 2

    def test_call_window_shorter_than_its_start_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "NEXT_STEP_CALL_UNTIL_DAYS", 3)
        recs = self.run_validate(caplog)
        assert any(r.levelno >= logging.WARNING and "NEXT_STEP_CALL_UNTIL_DAYS" in r.getMessage() for r in recs)

    def test_an_empty_connected_marker_list_warns_that_nobody_is_selected(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "NEXT_STEP_CONNECTED_MARKERS", [])
        recs = self.run_validate(caplog)
        assert any(r.levelno >= logging.WARNING and "NEXT_STEP_CONNECTED_MARKERS" in r.getMessage() for r in recs)
        tab = fx.parse([fx.person(1, li_date=C0, step="Research the PoC")])
        assert picked(run13(tab, date(2026, 10, 12))) == []

    def test_the_connected_marker_is_a_setting(self, monkeypatch):
        monkeypatch.setattr(config, "NEXT_STEP_CONNECTED_MARKERS", ["connected", "linked"])
        tab = fx.parse([fx.person(1, name="Via Setting", connected="Linked", li_date=C0, step="Research the PoC")])
        assert names(run13(tab, date(2026, 10, 12))) == ["Via Setting"]


# ---------------------------------------------------------------------------------------------
# what the builder touched that the plan did not list, and the post as a person reads it
# ---------------------------------------------------------------------------------------------

class TestTheLine:
    def test_a_row_with_no_name_is_named_by_its_company_alone(self):
        tab = fx.parse([fx.values_row(1, company="Acme Labs", connected="Connected", li_date=C0, step="Research the PoC")])
        it = picked(run13(tab, date(2026, 10, 12)))[0]
        assert it["text"].startswith("Acme Labs:") and "()" not in it["text"]

    def test_every_line_carries_the_persons_own_name_company_and_cells_only(self):
        tab = fx.parse([fx.person(1, name="Priya Rao", company="Acme Labs", li_date=C0, step="Send email 2",
                                  e1="yes", e1d=date(2026, 9, 5))])
        it = picked(run13(tab, date(2026, 10, 12)))[0]
        assert it["text"].startswith("Priya Rao (Acme Labs):")
        assert it["poc"] == "Priya Rao" and it["company"] == "Acme Labs"
        assert not re.search(r"https?://|@|\bR\d+\b", it["text"])

class TestThePost:
    def test_the_post_is_verbatim_never_composed_by_the_model(self):
        import drip
        m = next(m for m in drip.plan(r13_actions(THURSDAY), day=THURSDAY)["messages"] if m["type"] == TRIGGER)
        assert drip.is_verbatim(m) is True
        assert drip.nothing_to_say({**m, "actions": []}) is True

    def test_the_heading_is_next_steps(self):
        import drip
        assert drip.heading("R13") == "**Next steps**"

    def test_the_four_ways_share_one_renderer(self):
        """Live, test mode, test day and simulation all send `compose_fallback`'s text (verbatim): the same queue is
        the same body. (The bot-level proof is in verify_rule13.py (c) and verify_replay_oct6.py.)"""
        import drip
        a = next(m for m in drip.plan(r13_actions(THURSDAY), day=THURSDAY)["messages"] if m["type"] == TRIGGER)
        b = next(m for m in drip.plan(r13_actions(THURSDAY), day=THURSDAY)["messages"] if m["type"] == TRIGGER)
        assert drip.compose_fallback(a) == drip.compose_fallback(b)


class TestHardRules:
    def test_rule_13_never_proposes_or_writes_and_names_no_web_search(self):
        """Plan section 8.5: no web search, no email lookup, no proposal. Its items are not web-pending and carry no
        email-lookup request."""
        rows, _ = fx.rotation_values()
        for it in picked(run13(fx.parse(rows), fx.FIRST_DAY)):
            assert not it.get("web_pending") and not it.get("web_note")
            assert it["destination"] == "channel"


class TestPre7OctLayoutStillWorksForR9:
    """The pre-7 Oct row is kept as a fixture on purpose: R9 must read ITS notes column (S) there too."""

    def _row(self, notes):
        hdr = fx.HEADERS_PRE_7OCT
        r = [""] * len(hdr)
        for k, v in {"Name": "Priya Rao", "Company/Uni": "Acme Labs", "Meeting Date": "05-10-2026",
                     "Meeting Status": "Completed", "Next Steps/Notes": notes}.items():
            r[hdr.index(k)] = v
        return fx.parse([r], hdr)

    def test_blank_notes_are_followed_up(self):
        res = nextaction.run(today=date(2026, 10, 12), rows=list(self._row("").rows),
                             day_rules=[rules_mod.by_id("R9")])
        assert [a["poc"] for a in res["actions"]] == ["Priya Rao"]

    def test_filled_notes_are_silent(self):
        res = nextaction.run(today=date(2026, 10, 12), rows=list(self._row("sending the MSA").rows),
                             day_rules=[rules_mod.by_id("R9")])
        assert res["actions"] == []


class TestRecomputedEveryTick:
    """The queue is recomputed on every sweep tick. Once today's post has gone, the same five stay today's five:
    without that, the next tick would pick the NEXT five and the one-mention-a-day dedup would take them away from
    R5 and R6 for a post that is never sent (builder's addition beyond the plan; pinned here)."""

    def test_a_stopped_person_is_not_returned_by_the_same_day_replay(self, db):
        rows, meta = fx.rotation_values()
        day = fx.FIRST_DAY
        send(db, run13(fx.parse(rows), day, state=db.next_step_state()), day)
        row = next(r for r in rows if r[fx.HEADERS_7OCT.index("Name")] == meta["dated"][0])
        fx.edit(row, prospect="Unresponsive")                 # stopped after the post went out
        again = picked(run13(fx.parse(rows), day, state=db.next_step_state()))
        assert meta["dated"][0] not in poc_names(again)


class TestUnmappedColumns:
    """Found by the tester (sent to builder-r13): on a tab with no Next Steps column mapped, R13 used to ask 'Next
    Steps is blank' about every Connected person, which states something about the sheet the bot cannot see."""

    def _tab(self, headers, **cells):
        r = [""] * len(headers)
        for k, v in {"Name": "Priya Rao", "Company/Uni": "Acme Labs", "Sid - LI Addition": "Connected",
                     "LI Connected Date": "01-09-2026", **cells}.items():
            r[headers.index(k)] = v
        return fx.parse([r], headers)

    def test_the_pre_7oct_row_names_nobody_and_says_why(self, caplog):
        with caplog.at_level(logging.WARNING):
            res = run13(self._tab(fx.HEADERS_PRE_7OCT), date(2026, 10, 12))
        assert picked(res) == [] and res["reports"][R13]["state"] == "no_step_column"
        assert any("outreach_step" in r.getMessage() for r in caplog.records)

    def test_a_renamed_email_column_names_nobody_too(self):
        hdr = [h if h != "1st Email Date" else "Mail one date" for h in fx.HEADERS_7OCT]
        res = run13(self._tab(hdr), date(2026, 10, 12))
        assert picked(res) == [] and res["reports"][R13]["state"] == "no_step_column"

    def test_the_7oct_row_is_fine(self):
        res = run13(self._tab(fx.HEADERS_7OCT, **{"Next Steps": "Research the PoC"}), date(2026, 10, 12))
        assert names(res) == ["Priya Rao"] and res["reports"][R13]["state"] == "ok"


# ---------------------------------------------------------------------------------------------
# Y. REPLIES to a Rule 13 post (docs/plans/RULE13.md section 14, written from the plan, not from the code).
# A whole bot behind fake Discord (tests/replies_world.py: counts model calls, sheet writes, votes, proposals,
# reactions). Made-up people; the post goes out through the REAL planner and the REAL `_send_drip_message`.
# ---------------------------------------------------------------------------------------------

import asyncio  # noqa: E402

import replies_world as rw  # noqa: E402

DAY = date(2026, 10, 15)            # a Thursday

PEOPLE5 = [("Priya Rao", "Acme Labs"), ("Dev Shah", "Borealis"), ("Mei Lin", "Cinder"),
           ("Omar Khan", "Delta Co"), ("Lena Fox", "Echo Ltd")]


def _row(i, name, company, step, **cells):
    return fx.person(i, name=name, company=company, li_date=date(2026, 8, 20) + D(days=i), step=step, **cells)


def research_rows(n=2):
    return [_row(i + 1, *PEOPLE5[i], "Research the PoC") for i in range(n)]


def mixed_rows():
    """Priya on research, the other four on email steps."""
    rows = [_row(1, *PEOPLE5[0], "Research the PoC")]
    rows += [_row(i + 1, *PEOPLE5[i], "Send email 1") for i in range(1, 5)]
    return rows


def run(coro):
    return asyncio.run(coro)


def tables_of(w):
    with w.bot.db.conn() as c:
        return ([tuple(r) for r in c.execute("SELECT * FROM next_step_followups ORDER BY row_key").fetchall()],
                [tuple(r) for r in c.execute("SELECT * FROM next_step_posts ORDER BY message_id").fetchall()])


def r13_post(w, rows, day=DAY):
    """Send the real Rule 13 post for `rows`. Returns the BotMsg the post is."""
    import deadlines as dl
    w.sheet.set(pocs=rows)
    w.rules = {"R13"}

    async def go():
        planned = await w.bot._plan_drip(today=day, already=[])
        msg = next(m for m in planned["messages"] if m["type"] == TRIGGER)
        before = len(w.posted)
        await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(day), channel_id=w.chan.id)
        assert len(w.posted) > before, "the Rule 13 post was not sent"
        return w.posted[before]
    return run(go())


class Quiet:
    """What must NOT move while a reply is handled: the sheet, proposals, votes, the model, Rule 13's tables."""

    def __init__(self, w):
        self.w = w
        self.tables = tables_of(w)
        self.writes = len(w.sheet.writes)
        self.proposals = dict(w.proposals())
        self.votes = len(w.votes())
        self.model = w.model.calls

    def assert_unchanged(self, *, model=True):
        w = self.w
        assert len(w.sheet.writes) == self.writes, "a reply wrote to the sheet"
        assert w.proposals() == self.proposals, "a reply opened or moved a proposal"
        assert len(w.votes()) == self.votes, "a reply recorded a vote"
        assert tables_of(w) == self.tables, "a reply changed Rule 13's state"
        if model:
            assert w.model.calls == self.model, "a reply made a model call"


def said(w, text, post, **kw):
    """Say `text` as a reply to `post`; the bot's new messages, tag stripped."""
    n = w.n_posted
    run(w.say(text, reply_to=post, **kw))
    return w.replies_after(n)


LINE_RESEARCH = "Nice, can you set Next Steps for {short} to Send email 1?"
WHICH_2 = "Which one?\n1. Priya Rao (Acme Labs)\n2. Dev Shah (Borealis)\nA name or a number is fine."


class TestRepliesY:
    def test_Y6_every_reply_leaves_everything_alone(self):
        replies = ["done", "yes", "sure", "ok", "thanks", "👍", "not yet", "researched", "sent", "done for Priya",
                   "haven't sent it yet", "will do tomorrow", "which template should I use for Priya?"]
        for text in replies:
            with rw.World() as w:
                post = r13_post(w, mixed_rows())
                q = Quiet(w)
                said(w, text, post)
                q.assert_unchanged(model=False)
        for text in ("done", "yes", "sure", "researched", "done for Priya", "👍", "not yet"):
            with rw.World() as w:
                post = r13_post(w, mixed_rows())
                q = Quiet(w)
                said(w, text, post)
                q.assert_unchanged(model=True)

    def test_Y10_not_done_is_not_a_done(self):
        for text in ("haven't sent it yet", "will do tomorrow", "didn't get to it", "not done"):
            with rw.World() as w:
                post = r13_post(w, research_rows(1))
                got = said(w, text, post)
                assert not [g for g in got if g.startswith(("Nice, can you", "Thanks"))], (text, got)

    def test_Y11_no_posts_row_means_the_hook_steps_aside(self):
        with rw.World() as w:
            post = r13_post(w, research_rows(1))
            with w.bot.db.conn() as c:
                c.execute("DELETE FROM next_step_posts")
            got = said(w, "done", post)
            assert not [g for g in got if g.startswith("Nice, can you set Next Steps")], got

    def test_Y12_live_and_test_mode_say_the_same_apart_from_the_tag(self):
        out = {}
        for mode in (False, True):
            with rw.World(test_mode=mode) as w:
                post = r13_post(w, research_rows(2))
                n0 = w.n_posted
                run(w.say("done", reply_to=post))
                which = w.posted[-1]
                run(w.say("Dev", reply_to=which))
                out[mode] = ([rw.strip_tag(m.content) for m in w.posted[n0:]],
                             [m.content.startswith("[TEST") for m in w.posted[n0:]], w.model.calls,
                             len(w.sheet.writes))
        # identical, tag included: `_reply` does not tag an answer in either mode (plan 14.7 said it would; it does not,
        # only proactive posts carry [TEST...]), so there is no difference at all between the two
        assert out[False] == out[True], f"LIVE {out[False]!r} != TEST {out[True]!r}"


# ---------------------------------------------------------------------------------------------
# REMOVED ON 12 OCT 2026, WHEN RULE 13 BECAME A REMINDER. The tests below pinned behaviour that
# no longer exists: lines that asked for a cell to be filled, the "missing cells" list, the
# round-robin rotation, a connected row with no date being skipped, `closed` surviving a step
# change, and the reader for a "done" under the post. What replaced them is checked in
# tests/next_steps_checks.py (run by verify_next_steps.py and tests/test_next_steps.py).
#
#   TestCallClock:
#     test_C1_nothing_ever_again_even_after_q_changes
#     test_C4_default_f_a_replied_dm_never_gets_a_call_chase_or_the_unresponsive_reminder
#     test_C4_with_the_marker_list_emptied_a_replied_dm_follows_the_call_clock
#   TestClosedIsFinal:
#     test_the_shipped_default_a_closed_person_stays_closed_after_a_step_change
#     test_with_the_constant_off_a_step_change_reopens_them
#   TestRecomputedEveryTick:
#     test_after_the_post_the_same_five_come_back_until_tomorrow
#   TestRepliesY:
#     test_Y1_done_under_a_one_person_research_post
#     test_Y2_two_research_people_unnamed_done_asks_which_then_a_number_or_a_name_picks
#     test_Y2_all_picks_everyone
#     test_Y3_researched_picks_the_one_research_person_without_a_question
#     test_Y4_done_for_a_named_person_in_a_five_person_post
#     test_Y4_a_named_email_person_gets_the_email_line
#     test_Y5_one_exact_line_per_ask_code
#     test_Y5_every_reply_line_is_clean
#     test_Y7_an_approvers_yes_is_never_a_vote_and_applies_nothing
#     test_Y8_acknowledgements_get_one_reaction_and_no_text
#     test_Y9_a_real_question_goes_to_the_engine_with_the_post_as_context
#     test_Y13_a_non_approver_teammate_counts_default_d
#     test_a_shared_first_name_names_neither_and_the_full_name_is_used
#     test_the_reply_pure_helpers
#   TestRotation:
#     test_R1_five_a_day_in_sheet_order_wrapping_to_the_top
#     test_R1_nobody_comes_up_twice_before_every_due_person_has_had_a_turn
#     test_R1_the_four_undated_rows_are_skipped_listed_and_logged_every_day
#     test_R1_the_six_text_dates_and_every_step_are_in_the_rotation
#     test_R1_queued_people_are_reported_not_lost
#     test_R2_a_call_reminder_due_today_leads_the_post
#     test_R3_a_step_change_clears_that_persons_state_and_they_are_named_again_ahead_of_unseen_rows
#     test_R3_an_unchanged_step_is_not_named_early
#   TestSheetWordingDoc:
#     test_the_words_sent_to_vaishnavi_are_the_words_the_bot_loads
#   TestState:
#     test_a_changed_signature_resets_the_counts_but_never_the_closed_flag
#     test_posts_round_trip_for_nft2_1063
#   TestTheLine:
#     test_no_invented_date_a_line_that_needs_a_date_has_the_rows_own
#     test_the_item_carries_what_a_reply_will_need_later
#   TestThePost:
#     test_the_post_is_grouped_by_ask_and_is_never_random
#     test_a_call_reminder_stays_first_in_the_rendered_post
#   TestTimingTable:
#     test_T1_blank_step_is_due_two_days_after_the_connection
#     test_T2_research_is_due_two_days_after_the_connection
#     test_T3_email_1_not_sent_is_due_two_days_after_the_connection
#     test_T3_email_2_not_sent_is_due_two_days_after_the_first_email_date
#     test_T3_email_3_not_sent_is_due_two_days_after_the_second_email_date
#     test_T3_a_blank_previous_date_means_due_now_and_the_line_asks_for_it
#     test_T3_q_and_the_cells_disagree_go_by_q_and_ask_for_the_missing_cells
#     test_T3_email_3_with_nothing_logged_before_it_asks_for_both_earlier_pairs
#     test_T3_a_date_cell_with_text_that_is_not_a_date_counts_as_blank_and_is_named
#     test_T4_email_sent_with_a_date_is_due_a_week_later_and_asks_for_the_next_step
#     test_T4_the_connected_date_can_be_typed_as_DD_dot_MM_dot_YYYY
#     test_T5_email_sent_with_no_date_is_due_now_and_asks_for_the_date
#     test_T6_a_replied_dm_is_a_meeting_question_for_both_steps_and_never_a_call
#     test_T7_dm_step_without_a_dm_is_due_two_days_after_the_third_email
#     test_T7_a_blank_third_email_date_is_due_now_and_asks_for_it
#     test_T8_call_step_with_no_dm_logged_is_due_now_and_asks_for_the_dm_date
#     test_T9_T10_the_call_starts_seven_days_after_the_dm
#     test_T10_the_last_call_day_is_day_21_and_T11_day_22_is_the_unresponsive_reminder
#   TestWhoIsIn:
#     test_a_connected_row_with_no_date_is_skipped_logged_and_listed
#     test_a_stopped_row_is_out_at_every_step
#   TestWords:
#     test_V1_every_line_passes_the_register_and_carries_no_rule_number_or_schedule_talk
#     test_V1_every_group_ask_is_clean_and_in_all_lines
#     test_V1_what_is_personal_goes_after_the_colon
#     test_V1_the_grouped_post
# ---------------------------------------------------------------------------------------------
