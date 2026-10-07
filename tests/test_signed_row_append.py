"""NFT2-1065 addendum A-T3 / A-T4 / A-T6 / A-T7 — the REAL `GTMSheets.append_row` against a FAKE spreadsheet.

Written from docs/plans/NFT2-1065-addendum.md A1.2 and A4, not from the builder's code. The fake
spreadsheet records every call so the tests can say what was NOT done as well as what was:
  * a signed row is ONE `sh.batch_update` (values and note together) and never `ws.batch_update`;
  * without `note_role` the call is the old `ws.batch_update(..., USER_ENTERED)` and no note is made;
  * nothing outside the one new row is touched, and nothing is ever deleted or cleared because of a note.
No network: `SHEETS._open` returns the fake, and tests/offline_guard.py (autouse) blocks the real client.
"""
import re

import pytest

import config
import gtm_sheet

NOTE = ("Added by Saley · approved by Vaishnavi · 7 Oct 2026 IST\n"
        "Approval: https://discord.com/channels/1/4242/1234")


class FakeWs:
    id = 77

    def __init__(self, grid, *, fail_get_note=False, note_drops=False):
        self.grid = grid                       # list of rows (lists), row 1 first
        self.notes = {}
        self.batch_calls = []                  # ws.batch_update calls (the unsigned path)
        self.clear_calls = []
        self.fail_get_note = fail_get_note
        self.note_drops = note_drops           # a write whose note then reads back empty

    def get_all_values(self):
        return [list(r) for r in self.grid]

    def row_values(self, n):
        return list(self.grid[n - 1]) if 0 < n <= len(self.grid) else []

    def _set(self, row, col, text):
        while len(self.grid) < row:
            self.grid.append([])
        r = self.grid[row - 1]
        while len(r) <= col:
            r.append("")
        r[col] = text

    def batch_update(self, data, value_input_option=None):
        self.batch_calls.append((data, value_input_option))
        for d in data:
            m = re.fullmatch(r"([A-Z]+)(\d+)", d["range"])
            col = 0
            for ch in m.group(1):
                col = col * 26 + ord(ch) - 64
            self._set(int(m.group(2)), col - 1, d["values"][0][0])

    def get_note(self, cell):
        if self.fail_get_note:
            raise RuntimeError("get_note failed")
        return "" if self.note_drops else self.notes.get(cell, "")

    def clear_note(self, cell):
        self.clear_calls.append(cell)
        self.notes.pop(cell, None)


class FakeSh:
    def __init__(self, ws, *, raises=None):
        self.ws = ws
        self.calls = []                        # every spreadsheets.batchUpdate body
        self.raises = raises

    def worksheet(self, title):
        return self.ws

    def batch_update(self, body):
        self.calls.append(body)
        if self.raises:
            raise self.raises
        for req in body["requests"]:
            uc = req["updateCells"]
            rng = uc["range"]
            cell = uc["rows"][0]["values"][0]
            v = cell["userEnteredValue"]
            text = str(v["numberValue"]) if "numberValue" in v else v["stringValue"]
            self.ws._set(rng["startRowIndex"] + 1, rng["startColumnIndex"], text)
            if "note" in cell:
                col = rng["startColumnIndex"]
                letter = chr(65 + col)
                self.ws.notes[f"{letter}{rng['startRowIndex'] + 1}"] = cell["note"]


@pytest.fixture
def world(monkeypatch, pocs_tab):
    """(tab, ws, sh): the conftest PoCs tab plus a fake sheet whose grid matches it."""
    from conftest import POCS_HEADERS
    grid = [list(POCS_HEADERS)] + [[str(r.get("_row", "")) for _ in range(0)] for r in []]
    for row in pocs_tab.rows:
        line = [""] * len(POCS_HEADERS)
        for role, idx in (pocs_tab.canonical_role_to_col or {}).items():
            if idx < len(line):
                line[idx] = str(row.get(role) or "")
        while len(grid) < row["_row"] - 1:
            grid.append([""] * len(POCS_HEADERS))
        grid.append(line)
    ws = FakeWs(grid)
    sh = FakeSh(ws)
    monkeypatch.setattr(gtm_sheet.SHEETS, "_open", lambda which=None: sh)
    monkeypatch.setattr(config, "SHEET_WRITES_ENABLED", True)
    monkeypatch.setattr(config, "SHEET_APPENDABLE_TABS", ["outreach_pocs"])
    monkeypatch.setattr(config, "NEW_ROW_WRITABLE_RANGES", "A:R")
    return pocs_tab, ws, sh


VALUES = {"company": "ARTPARK India", "name": "Janajit Bagchi",
          "li_url": "https://www.linkedin.com/in/janajit-bagchi"}


def _append(tab, **kw):
    return gtm_sheet.SHEETS.append_row(tab, dict(VALUES), reason="test",
                                       expect_company="ARTPARK India", **kw)


def test_a_signed_row_is_one_batch_with_the_note_on_the_name_cell_only(world):
    tab, ws, sh = world
    before = [list(r) for r in ws.grid]
    out = _append(tab, note_role="name", note_text=NOTE)
    assert out["ok"] is True and out["signed"] is True
    assert len(sh.calls) == 1, "values and note must go in ONE spreadsheets.batchUpdate"
    assert ws.batch_calls == [], "the signed path must not use ws.batch_update"
    reqs = [r["updateCells"] for r in sh.calls[0]["requests"]]
    with_note = [r for r in reqs if "note" in r["rows"][0]["values"][0]]
    assert len(with_note) == 1
    assert with_note[0]["fields"] == "userEnteredValue,note"
    assert all(r["fields"] == "userEnteredValue" for r in reqs if r is not with_note[0])
    name_col = tab.canonical_role_to_col["name"]
    assert with_note[0]["range"]["startColumnIndex"] == name_col
    assert list(ws.notes) == [out["note_cell"]]                      # one note, on the new row's Name cell
    assert ws.notes[out["note_cell"]] == NOTE
    assert out["note_cell"].endswith(str(out["sheet_row"]))
    # nothing else on the sheet moved
    new_row = out["sheet_row"]
    for i, row in enumerate(before, start=1):
        assert ws.grid[i - 1] == row, f"row {i} changed"
    assert ws.clear_calls == []


def test_serial_goes_as_a_number_and_text_never_as_a_formula(world):
    tab, ws, sh = world
    _append(tab, note_role="name", note_text=NOTE)
    reqs = [r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] for r in sh.calls[0]["requests"]]
    assert any("numberValue" in v for v in reqs)                    # the Sr No
    assert all(("numberValue" in v) or ("stringValue" in v) for v in reqs)
    tab2 = tab
    out = gtm_sheet.SHEETS.append_row(
        tab2, {"company": "Acme", "name": "=HYPERLINK(\"x\")"}, reason="t", note_role="name", note_text=NOTE)
    last = sh.calls[-1]["requests"]
    texts = [r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] for r in last]
    assert {"stringValue": "=HYPERLINK(\"x\")"} in texts, "a value starting with = must stay text"
    assert out["ok"] is True


def test_the_row_carries_only_serial_company_name_and_url(world):
    tab, ws, sh = world
    out = _append(tab, note_role="name", note_text=NOTE)
    roles = sorted(c["role"] for c in out["written"])
    assert roles == ["company", "li_url", "name", "sr_no"]
    assert not any(c["role"] == "name" and NOTE in str(c["value"]) for c in out["written"])
    # no signature text in ANY data cell of the new row
    assert all("Added by Saley" not in cell for cell in ws.grid[out["sheet_row"] - 1])


def test_an_empty_linkedin_url_writes_no_cell_for_it(world):
    tab, ws, sh = world
    out = gtm_sheet.SHEETS.append_row(tab, {"company": "ARTPARK India", "name": "Suryansh Shukla", "li_url": ""},
                                      reason="t", note_role="name", note_text=NOTE)
    assert "li_url" not in {c["role"] for c in out["written"]}


def test_without_note_role_it_is_the_old_ws_batch_update(world):
    tab, ws, sh = world
    out = _append(tab)
    assert out["ok"] is True
    assert sh.calls == [] and len(ws.batch_calls) == 1
    assert ws.batch_calls[0][1] == "USER_ENTERED"
    assert ws.notes == {} and "signed" not in out


def test_the_batch_raising_leaves_nothing_and_clears_nothing(world):
    tab, ws, sh = world
    before = [list(r) for r in ws.grid]
    sh.raises = RuntimeError("boom")
    out = _append(tab, note_role="name", note_text=NOTE)
    assert out["ok"] is False and out["error"]
    assert ws.grid == before and ws.notes == {}
    assert ws.batch_calls == [] and ws.clear_calls == []


def test_a_note_that_does_not_read_back_keeps_the_row_and_flags_it(world):
    tab, ws, sh = world
    ws.note_drops = True
    out = _append(tab, note_role="name", note_text=NOTE)
    assert out["ok"] is True and out["signed"] is False and out["note_error"]
    assert out["sheet_row"] > 0
    assert any("Janajit Bagchi" in cell for cell in ws.grid[out["sheet_row"] - 1])    # the row stayed
    assert ws.clear_calls == [] and ws.batch_calls == []                                # nothing cleared


def test_get_note_failing_is_keep_and_flag_too(world):
    tab, ws, sh = world
    ws.fail_get_note = True
    out = _append(tab, note_role="name", note_text=NOTE)
    assert out["ok"] is True and out["signed"] is False and out["note_error"]
    assert ws.clear_calls == []


def test_no_name_cell_refuses_before_any_write(world):
    tab, ws, sh = world
    before = [list(r) for r in ws.grid]
    out = gtm_sheet.SHEETS.append_row(tab, {"company": "ARTPARK India", "name": ""}, reason="t",
                                      note_role="name", note_text=NOTE)
    assert out["ok"] is False and "cannot sign" in out["error"]
    assert sh.calls == [] and ws.batch_calls == [] and ws.grid == before


def test_a_note_role_with_no_text_is_refused_too(world):
    tab, ws, sh = world
    out = _append(tab, note_role="name", note_text="")
    assert out["ok"] is False and sh.calls == [] and ws.batch_calls == []


def test_dry_run_writes_no_note(world):
    tab, ws, sh = world
    out = _append(tab, note_role="name", note_text=NOTE, dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    assert sh.calls == [] and ws.batch_calls == [] and ws.notes == {}
    assert out.get("would_sign")


def test_a_duplicate_is_refused_and_nothing_is_written(world):
    tab, ws, sh = world
    out = gtm_sheet.SHEETS.append_row(tab, {"company": "Wispr Flow", "name": "Sahaj Garg"}, reason="t",
                                      note_role="name", note_text=NOTE)
    assert out["ok"] is False and out["duplicate"] is not None
    assert sh.calls == [] and ws.notes == {}


def test_the_commercial_block_is_still_refused_on_a_signed_row(world):
    tab, ws, sh = world
    out = gtm_sheet.SHEETS.append_row(tab, {"company": "Nova", "name": "Someone", "closure_prob": "80%"},
                                      reason="t", note_role="name", note_text=NOTE)
    assert "closure_prob" in {r["role"] for r in out.get("refused", [])}
    assert "closure_prob" not in {c["role"] for c in out.get("written", [])}


def test_fill_serial_false_leaves_the_serial_cell_blank(world):
    tab, ws, sh = world
    out = _append(tab, note_role="name", note_text=NOTE, fill_serial=False)
    assert "sr_no" not in {c["role"] for c in out["written"]}
    out2 = _append_other(tab)
    assert "sr_no" in {c["role"] for c in out2["written"]}      # the default is unchanged (events and every other caller)


def _append_other(tab):
    return gtm_sheet.SHEETS.append_row(tab, {"company": "Other Org", "name": "Other Person"}, reason="t",
                                       note_role="name", note_text=NOTE)


def test_fill_serial_false_on_the_signed_path_has_no_request_for_column_a(world):
    tab, ws, sh = world
    _append(tab, note_role="name", note_text=NOTE, fill_serial=False)
    cols = sorted(r["updateCells"]["range"]["startColumnIndex"] for r in sh.calls[0]["requests"])
    assert tab.canonical_role_to_col["sr_no"] not in cols
    assert cols == sorted([tab.canonical_role_to_col[k] for k in ("company", "name", "li_url")])


def test_an_explicit_sr_no_is_written_either_way(world):
    tab, ws, sh = world
    for flag in (True, False):
        out = gtm_sheet.SHEETS.append_row(
            tab, {"company": f"Org{flag}", "name": f"P{flag}", "sr_no": "99"}, reason="t",
            note_role="name", note_text=NOTE, fill_serial=flag)
        assert "sr_no" in {c["role"] for c in out["written"]}
        assert [c["value"] for c in out["written"] if c["role"] == "sr_no"] == ["99"]


def test_the_events_caller_passes_no_fill_serial():
    import inspect
    import bot
    src = inspect.getsource(bot.SalesBot._apply_event_append)
    assert "fill_serial" not in src and "note_role" not in src


def test_a_url_column_outside_the_new_row_band_is_refused_and_said(world, monkeypatch):
    tab, ws, sh = world
    monkeypatch.setattr(config, "NEW_ROW_WRITABLE_RANGES", "A:H")
    monkeypatch.setattr(config, "_NEW_ROW_INDEXES", None)      # the parsed band is cached; re-parse it
    out = _append(tab, note_role="name", note_text=NOTE + chr(10) + "LinkedIn link found by web search: " + VALUES["li_url"])
    assert out["ok"] is True and out["signed"] is True
    assert [r["role"] for r in out["refused"]] == ["li_url"]
    assert "li_url" not in {c["role"] for c in out["written"]}
    assert "NEW_ROW_WRITABLE_RANGES" in out["refused"][0]["why"]
    assert "LinkedIn link found by web search" in ws.notes[out["note_cell"]]       # the note keeps the link
    assert VALUES["li_url"] not in " ".join(ws.grid[out["sheet_row"] - 1])           # the cell stayed empty
