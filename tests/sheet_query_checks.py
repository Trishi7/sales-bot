"""NFT2-1095 "SALEY CANNOT ANSWER GENERAL SHEET QUESTIONS" — the checks, shared by verify_sheet_query.py (which
prints the T1-T17 table) and tests/test_sheet_query.py.

Every check is named for the test case it covers ("T3 ...") or the edge case ("E7 ..."). The tools under test are
the REAL `sheet_columns` / `sheet_query` / `query_tracker` / `lookup_company` handlers from `SalesBot._sheet_tools`,
over the REAL sheet parser, inside tests/replies_world.py's world (fake Discord, a throwaway database, a stand-in
sheet that never touches Google). The fixture is tests/fixture_outreach_2026_10_13.py: the Outreach PoCs tab as it
stood on 13 Oct 2026 (no emails, no phone numbers) and the real Master Pipeline tab.

T10 runs the 15 Oct question through the real `on_message` -> engine loop, with a stand-in model that picks the
tools the way the prompt tells it to and renders the bullets from what the tool returned.
"""
import asyncio
import contextlib
import copy
import inspect
import json
import os
import re
import subprocess
import sys
import types
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fixture_outreach_2026_10_13 as fx  # noqa: E402
import replies_world as rw  # noqa: E402

QUESTION = ("share list of PoCs whose Next Steps status is 'research the poc', and are based out of UK or USA? "
            "share PoC's Name / Company/Uni Name / Designation in bullet points")
UK_USA = ["UK", "USA", "United Kingdom", "United States", "London"]
RESEARCH = {"column": "Next Steps", "op": "equals", "value": "research the poc"}
BASED = {"column": "Based", "op": "contains_any", "value": UK_USA}
THREE = ["Name", "Company/Uni", "Designation"]
NAME, COMPANY, TITLE = "Name (name)", "Company/Uni (company)", "Designation (designation)"


def grids(pocs=None, master=None, extra=None) -> dict:
    out = {"Outreach PoCs": pocs if pocs is not None else fx.grid(),
           "Master Pipeline": master if master is not None else fx.master_grid()}
    out.update(extra or {})
    return out


@contextlib.contextmanager
def sheet_world(*, test_mode=False, model=None, pocs=None, master=None, extra=None):
    """A world whose engine has the REAL sheet tools over the fixture."""
    from bot import SalesBot
    with rw.World(test_mode=test_mode, model=model) as w:
        w.sheet.grids = grids(pocs, master, extra)
        w.bot._sheet_tools = types.MethodType(SalesBot._sheet_tools, w.bot)
        yield w


def handlers(w) -> dict:
    return {t["schema"]["name"]: t["handler"] for t in w.bot._sheet_tools(None)}


def schemas(w) -> dict:
    return {t["schema"]["name"]: t["schema"] for t in w.bot._sheet_tools(None)}


async def call(w, name, inp):
    return await handlers(w)[name](inp)


def names_of(res) -> list:
    return [r.get(NAME) for r in res.get("rows") or []]


# -- T1-T9, T13, the shape of a query --------------------------------------------------------------------------

async def columns_and_queries(check) -> None:
    with sheet_world() as w:
        cols = await call(w, "sheet_columns", {"tab": "Outreach PoCs"})
        check("T1 sheet_columns: 35 headers, in sheet order",
              ([c["header"] for c in cols["columns"]], cols["column_count"]), (fx.HEADERS, 35))
        step = next(c for c in cols["columns"] if c["header"] == "Next Steps")
        check("T1 ...'Next Steps' is column P, role outreach_step", (step["letter"], step["role"]),
              ("P", "outreach_step"))
        check("T1 ...and shows its 5 values with counts",
              {v["value"]: v["count"] for v in step["values"]}, fx.NEXT_STEPS_13_OCT)
        notes = next(c for c in cols["columns"] if c["header"] == "Notes/Remarks")
        check("T1 ...'Notes/Remarks' is column AC, role next_steps (the free text)", (notes["letter"], notes["role"]),
              ("AC", "next_steps"))
        title = next(c for c in cols["columns"] if c["header"] == "Designation")
        check("T1 ...a free-text column (600-odd designations) carries no value list", "values" in title, False)

        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "where": [RESEARCH]})
        check("T2 Next Steps equals 'research the poc': 20 rows", (res["match_count"], res["returned"]), (20, 20))
        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "where": [RESEARCH, BASED]})
        check("T3 ...and Based contains_any [UK, USA, United Kingdom, United States, London]: the 8, in sheet order",
              names_of(res), fx.T3_NAMES)
        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "where": [RESEARCH, BASED], "columns": THREE})
        check("T4 columns = Name, Company/Uni, Designation: only those three per row",
              {tuple(sorted(r)) for r in res["rows"]}, {tuple(sorted([NAME, COMPANY, TITLE]))})
        check("T4 ...in the order asked", res["columns"], [NAME, COMPANY, TITLE])
        first = res["rows"][0]
        check("T4 ...the cells are the sheet's own text",
              (first[NAME], first[COMPANY], first[TITLE].startswith("Principal Research Scientist")),
              ("Piotr Żelasko", "NVIDIA", True))
        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "where": [RESEARCH, BASED]})
        check("T4 ...and with no columns asked for, the same three by default", res["columns"], [NAME, COMPANY, TITLE])

        res = await call(w, "sheet_query", {"where": [{"column": "Next Steps", "op": "equals", "value": "researching"}]})
        check("T5/E2 'researching': 0 rows", res["match_count"], 0)
        check("T5/E2 ...and the closest value is suggested",
              (res.get("did_you_mean"), res.get("closest")),
              ("Did you mean Research the PoC?", {"Next Steps (outreach_step)": ["Research the PoC"]}))

        res = await call(w, "sheet_query", {"where": [{"column": "Meeting Date", "op": "after",
                                                        "value": "2026-10-01"}], "columns": ["Name", "Meeting Date"]})
        check("T6/E4 Meeting Date after 2026-10-01 on the fixture: only 02-10-2026 (day first), 0 unreadable",
              ([(r[NAME], r["Meeting Date (meeting_date)"]) for r in res["rows"]], res["unreadable_dates"]),
              ([("Sujith P", "02-10-2026")], 0))

        res = await call(w, "sheet_query", {"where": [{"column": "Deal Status", "op": "not_empty"}],
                                            "columns": ["Name", "Deal Status"]})
        tab = fx.tab()
        want = sum(1 for r in tab.rows if (r.get("deal_status") or "").strip())
        check("T7 Deal Status not_empty: only rows with a deal status, all of them",
              (res["match_count"], all(r["Deal Status (deal_status)"] for r in res["rows"])), (want, True))

        res = await call(w, "sheet_query", {"where": [{"column": "First Contact", "op": "equals", "value": "FALSE"}],
                                            "limit": 300})
        check("T8/E5 limit 300: capped at 200, match_count is the total",
              (res["limit"], res["returned"], res["truncated"], res["match_count"]), (200, 200, True, 543))
        check("T8/E5 ...and it says so", ("limit_capped" in res, "543" in res.get("note", "")), (True, True))
        res = await call(w, "sheet_query", {"where": [{"column": "First Contact", "op": "equals", "value": "FALSE"}]})
        check("T8 ...the default limit is 50", (res["limit"], res["returned"]), (50, 50))

        res = await call(w, "sheet_query", {"where": [{"column": "Nonsense", "op": "equals", "value": "x"}]})
        check("T9/E7 column 'Nonsense': an error naming the tab's real columns",
              ("Nonsense" in res.get("error", ""), res.get("columns")), (True, fx.HEADERS))
        res = await call(w, "sheet_query", {"where": [RESEARCH], "columns": ["Nonsense"]})
        check("T9/E7 ...the same for a column asked for in 'columns'", res.get("columns"), fx.HEADERS)
        res = await call(w, "sheet_query", {"where": [{"column": "Name", "op": "like", "value": "x"}]})
        check("T9 ...an op that does not exist names the ops", res.get("ops"), list(__import__("sheetquery").OPS))

        lk = await call(w, "lookup_company", {"company": "AI Security Institute"})
        row = lk["rows"][0]
        check("T13 lookup_company: keys are sheet headers with the role in brackets",
              (row.get("Next Steps (outreach_step)"), "Notes/Remarks (next_steps)" in row["empty"],
               row.get("Company/Uni (company)")),
              ("Research the PoC", True, "AI Security Institute (UK Govt.)"))
        check("T13 ...a column with no role is keyed by its header alone", "About PoC" in row["empty"], True)
        check("T13 ...the old role-keyed shape is gone", any(k in row for k in ("fields", "empty_fields",
                                                                              "other_columns")), False)
        import sheetquery
        every = {"sheet_row", "empty"} | {sheetquery.key(tab, i) for i, h in enumerate(tab.headers) if h.strip()}
        qt = await call(w, "query_tracker", {"filter": "meetings"})
        for label, r in (("lookup_company", row), ("query_tracker", qt["rows"][0])):
            check(f"T13 {label}: every column is there, filled under its key or named in 'empty'",
                  (set(r) | set(r["empty"])) - {"sheet_row", "empty"} | {"sheet_row", "empty"}, every)
        check("T13 ...header (role) keys in query_tracker too", "Meeting Date (meeting_date)" in qt["rows"][0], True)
        res = await call(w, "sheet_query", {"where": [RESEARCH], "columns": ["Next Steps", "Notes/Remarks"]})
        check("T13 sheet_query keys the dropdown and the notes apart",
              res["columns"], ["Next Steps (outreach_step)", "Notes/Remarks (next_steps)"])
        for name in ("lookup_company", "query_tracker"):
            desc = schemas(w)[name]["description"]
            check(f"T13 the {name} description names the new shape", "(outreach_step)" in desc
                  or "same shape as lookup_company" in desc, True)

        # E3: the ways "UK or USA" is written
        res = await call(w, "sheet_query", {"where": [RESEARCH, {"column": "Based", "op": "contains_any",
                                                                  "value": ["UK", "United Kingdom", "London", "USA",
                                                                            "United States", "US"]}]})
        check("E3 the list the model builds for 'UK or USA' (US included) finds the same 8", names_of(res),
              fx.T3_NAMES)
        res = await call(w, "sheet_query", {"where": [{"column": "Based", "op": "contains", "value": "UK"}],
                                            "limit": 200})
        check("E3 a short word is a whole word: 'UK' never matches Ukraine",
              any("ukrain" in str(r).lower() for r in res["rows"]), False)

        # E9: the 327 FALSE-only rows
        check("E9 the fixture carries the 327 rows that hold only FALSE in First Contact",
              sum(1 for r in fx.grid()[1:] if [c for c in r if c] == ["FALSE"]), 327)
        check("E9 ...and the parser skips them, as every reader does", len(tab.rows), len(fx.ROWS))
        res = await call(w, "sheet_query", {"where": [{"column": "First Contact", "op": "equals", "value": "FALSE"}]})
        check("E9 ...so 'First Contact equals FALSE' counts 543 PoCs, not 543 + 327", res["match_count"], 543)

    # E4 again on a tab with mixed date formats: unreadable cells are counted, never guessed
    g = fx.grid()
    md = fx.HEADERS.index("Meeting Date")
    typed = ["5 Oct 2026", "Oct 20, 2026", "TBD", "next week", "03/10/2026", "30-09-2026", "FALSE"]
    rows_with = [i for i, r in enumerate(g) if i and r[1]][:len(typed)]
    for i, text in zip(rows_with, typed):
        g[i][md] = text
    with sheet_world(pocs=g) as w:
        res = await call(w, "sheet_query", {"where": [{"column": "Meeting Date", "op": "after", "value": "01/10/2026"}],
                                            "columns": ["Meeting Date"]})
        got = sorted(r["Meeting Date (meeting_date)"] for r in res["rows"])
        check("T6/E4 mixed formats, day first: 5 Oct, 20 Oct, 3 Oct and the fixture's 2 Oct are after 1 Oct",
              got, sorted(["5 Oct 2026", "Oct 20, 2026", "03/10/2026", "02-10-2026"]))
        check("T6/E4 ...'TBD' and 'next week' are skipped and counted; FALSE is empty, not unreadable",
              res["unreadable_dates"], 2)
        check("T6/E4 ...and the result says so", "2 date cell(s) could not be read" in res.get("note", ""), True)
        res = await call(w, "sheet_query", {"where": [{"column": "Meeting Date", "op": "before", "value": "1 Oct 2026"}],
                                            "columns": ["Meeting Date"]})
        check("T6 ...before 1 Oct: 30-09-2026 and 08/09/2026 (8 September, day first)",
              sorted(r["Meeting Date (meeting_date)"] for r in res["rows"]), ["08/09/2026", "30-09-2026"])

    # E10: a header that changes
    g = fx.grid()
    ns = fx.HEADERS.index("Next Steps")
    moved = [r[:ns] + r[ns + 1:] + [r[ns]] for r in g]
    with sheet_world(pocs=moved) as w:
        res = await call(w, "sheet_query", {"where": [RESEARCH, BASED]})
        cols = await call(w, "sheet_columns", {})
        step = next(c for c in cols["columns"] if c["header"] == "Next Steps")
        check("E10 Next Steps moved to the last column: found by its header, the same 8",
              (step["letter"], names_of(res)), ("AI", fx.T3_NAMES))
    g = fx.grid()
    g[0][fx.HEADERS.index("Reconnect Date")] = "Follow-up"
    with sheet_world(pocs=g) as w:
        cols = await call(w, "sheet_columns", {})
        check("E10 a role that maps to no column is reported, not guessed",
              ("reconnect_date" in cols.get("roles_not_found", []), "roles_note" in cols), (True, True))
        res = await call(w, "sheet_query", {"where": [{"column": "reconnect_date", "op": "not_empty"}]})
        check("E10 ...and a query on it names the columns that do exist",
              ("Follow-up" in (res.get("columns") or []), "reconnect_date" in res.get("error", "")), (True, True))


# -- T10, the 15 Oct question through the engine -----------------------------------------------------------------

class Brain(rw.ScriptModel):
    """Picks the tools the prompt tells it to (sheet_columns, then sheet_query), then renders the bullets from
    what sheet_query returned. It never knows the answer in advance."""

    def __init__(self):
        super().__init__()
        self.script = [("use", [("sheet_columns", {"tab": "Outreach PoCs"})]),
                       ("use", [("sheet_query", {"tab": "Outreach PoCs", "where": [RESEARCH, BASED],
                                                 "columns": THREE})])]
        self.rendered_from = None

    def create(self, **kw):
        if not self.script:
            last = None
            for m in kw.get("messages") or []:
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            raw = b.get("content")
                            raw = raw if isinstance(raw, str) else json.dumps(raw)
                            with contextlib.suppress(ValueError):
                                parsed = json.loads(raw)
                                if "match_count" in parsed:
                                    last = parsed
            self.rendered_from = last
            if last:
                keys = last["columns"]
                lines = [f"{last['match_count']} PoCs have Next Steps = Research the PoC and are based in the UK "
                         "or the USA:"]
                for r in last["rows"]:
                    lines.append("- " + " / ".join(" ".join(str(r[k]).split()) for k in keys))
                self.answer = "\n".join(lines)
            else:
                self.answer = "I couldn't read that."
        return super().create(**kw)


def system_text(req) -> str:
    sys_ = req.get("system")
    if isinstance(sys_, str):
        return sys_
    return "\n".join(str(b.get("text") or "") for b in (sys_ or []) if isinstance(b, dict))


async def engine(check) -> None:
    replies = {}
    for test_mode in (False, True):
        brain = Brain()
        with sheet_world(test_mode=test_mode, model=brain) as w:
            n = w.n_posted
            await w.say(QUESTION, who="approver", mention=True)
            out = w.replies_after(n)
            replies[test_mode] = out
            tag = "test mode" if test_mode else "live"
            check(f"T10 [{tag}] the engine was offered sheet_columns and sheet_query",
                  all(t in (brain.offered[0] if brain.offered else []) for t in ("sheet_columns", "sheet_query")), True)
            called = [name for name, _ in brain.results]
            check(f"T10 [{tag}] it called sheet_columns, then sheet_query", called, ["sheet_columns", "sheet_query"])
            res = dict(brain.results)["sheet_query"] if called else {}
            check(f"T10 [{tag}] sheet_query found the 8", (res.get("match_count"), names_of(res)), (8, fx.T3_NAMES))
            text = "\n".join(out)
            bullets = [l for l in text.splitlines() if l.lstrip().startswith(("- ", "• ", "* "))]
            check(f"T10 [{tag}] the reply is a bullet list of the 8: Name / Company/Uni / Designation",
                  [b.split(" / ")[0].lstrip("-•* ").strip() for b in bullets], fx.T3_NAMES)
            check(f"T10 [{tag}] ...each bullet has all three parts",
                  all(len(b.split(" / ")) >= 3 for b in bullets), True)
            check(f"T10 [{tag}] ...Katarina Slama's line is the sheet's own",
                  any("Katarina Slama / AI Security Institute (UK Govt.) / AI Research Scientist" in b for b in bullets),
                  True)
            check(f"T10 [{tag}] ...and the match count is given", "8 PoCs" in text, True)
            check(f"T10 [{tag}] no 'can't filter' answer", bool(re.search(r"can.?t (filter|slice)|which column",
                                                                          text, re.I)), False)
            prompt = system_text(brain.requests[0]) if brain.requests else ""
            check(f"T10/E8 [{tag}] the prompt carries the sheet rule (columns first, never 'can't slice', the "
                  "two-tab join, bullets and the count)",
                  all(s in prompt for s in ("QUESTIONS ABOUT ROWS ON THE SHEET", "call sheet_columns",
                                            "NEVER ANSWER \"I can't slice by that\"", "join them by company name",
                                            "give the match count first")), True)
            check(f"T10 [{tag}] nothing was written", w.sheet.writes, [])
    check("T10 test mode and live give the same reply (apart from the tag)", replies[True], replies[False])

    # the rule is only sent with its tool
    import query_engine
    check("T10 a question with no sheet_query offered gets no sheet rule",
          "QUESTIONS ABOUT ROWS ON THE SHEET" in query_engine._engine_text(
              requester_name="V", today="2026-10-15", tool_names=["web_search"]), False)


# -- T11, read-only ------------------------------------------------------------------------------------------------

def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout


async def read_only(check) -> None:
    import gtm_sheet
    import sheetquery
    with sheet_world() as w:
        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "set": {"P2": "Send email 1"}})
        check("T11 a 'set' key is refused by name", (res.get("read_only"), "set" in res.get("error", "")), (True, True))
        res = await call(w, "sheet_query", {"where": [{"column": "Next Steps", "op": "update",
                                                        "value": "Send email 1"}]})
        check("T11 an 'update' op is refused", res.get("read_only"), True)
        res = await call(w, "sheet_query", {"tab": "Outreach PoCs", "values": [["x"]], "range": "P2"})
        check("T11 'values' + 'range' are refused", res.get("read_only"), True)
        res = await call(w, "sheet_columns", {"tab": "Outreach PoCs", "write": "x"})
        check("T11 sheet_columns refuses a write too", res.get("read_only"), True)
        check("T11 ...the refusal says writes go through an approval proposal",
              "approval proposal" in res.get("error", ""), True)
        await call(w, "sheet_query", {"where": [RESEARCH]})
        await call(w, "sheet_columns", {})
        check("T11 nothing reached the sheet's write paths", w.sheet.writes, [])
        for name in ("sheet_query", "sheet_columns"):
            src = inspect.getsource(handlers(w)[name])
            check(f"T11 the {name} handler has no write call",
                  [s for s in ("write_cells", "append_row", "write_email", "clear_cells", "batch_update", "sheetwrite",
                               "undo_cells") if s in src], [])
            schema = schemas(w)[name]
            check(f"T11 the {name} schema has no write field",
                  sorted(set(schema["input_schema"]["properties"]) & set(sheetquery.WRITE_KEYS)), [])
    src = open(os.path.join(REPO, "sheetquery.py"), encoding="utf-8").read()
    check("T11 sheetquery.py imports and calls no write path",
          [s for s in ("write_cells", "append_row", "write_email", "clear_cells", "batch_update", "import sheetwrite",
                       "undo_cells", "SHEETS.write", "SHEETS.append") if s in src], [])
    check("T11 the role-based column locks are untouched (config.py, gtm_sheet.py, sheetwrite.py as on main)",
          _git("diff", "--stat", "main", "--", "config.py", "gtm_sheet.py", "sheetwrite.py").strip(), "")
    check("T11 every sheet write still goes through an approval proposal (approvals.py as on main)",
          _git("diff", "--stat", "main", "--", "approvals.py").strip(), "")
    check("HARD LIMIT the daily post templates, the reply reader and the news flow are untouched",
          _git("diff", "--stat", "main", "--", "drip.py", "wording.py", "rules.py", "nextaction.py", "replies.py",
               "replyguard.py", "news.py", "news_screen.py", "feeds.py", "arxiv.py").strip(), "")
    check("T11 the lock still names its roles", bool(getattr(gtm_sheet, "ROLES", None)), True)


# -- T12, query_tracker unchanged ----------------------------------------------------------------------------------

def old_query_tracker(tab, inp) -> list:
    """`_query_tracker`'s filters AS THEY WERE ON MAIN (84c0f47), copied verbatim: the reference T12 compares to."""
    import tracker
    rows = list(tab.rows)
    filt = str(inp.get("filter") or "all").strip().lower()
    if filt == "no_response":
        rows = [r for r in rows if not tracker.has_responded(r) and tracker.is_open(r)]
    elif filt == "responded":
        rows = [r for r in rows if tracker.has_responded(r)]
    elif filt == "never_contacted":
        rows = [
            r for r in rows
            if not (r.get("first_contacted") or "").strip()
            and not (r.get("intro_date") or "").strip()
        ]
    elif filt == "no_next_step":
        rows = [r for r in rows if not (r.get("next_steps") or "").strip()]
    elif filt == "no_poc":
        rows = [r for r in rows if not (r.get("poc") or "").strip()]
    elif filt == "meetings":
        rows = [r for r in rows if (r.get("meeting_date") or "").strip()]
    elif filt == "open":
        rows = [r for r in rows if tracker.is_open(r)]
    industry = str(inp.get("industry") or "").strip().lower()
    if industry:
        rows = [r for r in rows if industry in (r.get("industry") or "").lower()]
    use_case = str(inp.get("use_case") or "").strip().lower()
    if use_case:
        rows = [r for r in rows if use_case in (r.get("use_case") or "").lower()]
    return rows


async def tracker_filters(check) -> None:
    import canned_sheet
    filters = ["all", "open", "no_response", "responded", "never_contacted", "no_next_step", "no_poc", "meetings"]
    for label, pocs in (("13 Oct fixture", fx.grid()), ("7 Oct canned sheet", canned_sheet.pocs_values())):
        with sheet_world(pocs=pocs) as w:
            tab = w.sheet.read()[__import__("gtm_sheet").POCS]
            for f in filters:
                for extra in ({}, {"industry": "AI"}):
                    inp = dict({"filter": f, "limit": 60}, **extra)
                    new = await call(w, "query_tracker", inp)
                    old = old_query_tracker(tab, inp)
                    check(f"T12 [{label}] query_tracker filter={f}{' industry=AI' if extra else ''}: same rows as "
                          "before", (new["match_count"], [r["sheet_row"] for r in new["rows"]]),
                          (len(old), [r["_row"] for r in old[:60]]))
            schema = schemas(w)["query_tracker"]
            check(f"T12 [{label}] the 8 filters are still the enum", schema["input_schema"]["properties"]["filter"]
                  ["enum"], filters)
    check("T12 the no_next_step filter now says it reads Notes/Remarks",
          "Notes/Remarks" in schemas_of_bot()["query_tracker"]["description"], True)


def schemas_of_bot() -> dict:
    with sheet_world() as w:
        return schemas(w)


# -- T14, T15, E6: two columns, the Master Pipeline, a tab that is not readable -------------------------------------

async def tabs_and_columns(check) -> None:
    import canned_sheet
    import gtm_sheet
    old = list(canned_sheet.POCS_HEADERS)
    old[old.index("Notes/Remarks")] = "Next Steps/Notes"
    grid = [old] + canned_sheet.pocs_values()[1:]
    step = old.index("Next Steps")
    grid[1][step] = "Research the PoC"
    with sheet_world(pocs=grid) as w:
        res = await call(w, "sheet_query", {"where": [RESEARCH]})
        check("T14/E1 two columns match 'Next Steps': both returned, no rows, no guess",
              ([c["header"] for c in res.get("ambiguous") or []], "rows" in res),
              (["Next Steps", "Next Steps/Notes"], False))
        check("T14/E1 ...and a question back", res.get("question", "").endswith("Which one did you mean?"), True)
        cols = await call(w, "sheet_columns", {})
        check("T14 sheet_columns shows both, with their roles",
              [(c["header"], c["role"]) for c in cols["columns"] if "next" in c["header"].lower()],
              [("Next Steps", "outreach_step"), ("Next Steps/Notes", "next_steps")])
        res = await call(w, "sheet_query", {"where": [dict(RESEARCH, column="Next Steps (outreach_step)")]})
        check("T14 ...the answer to the question ('Next Steps (outreach_step)') then works", res["match_count"], 1)

        # T15 on the real Master Pipeline: it has no Priority column
    with sheet_world() as w:
        res = await call(w, "sheet_query", {"tab": "Master Pipeline", "where": [{"column": "Priority", "op": "equals",
                                                                                "value": "P1"}]})
        check("T15 the REAL Master Pipeline (15 Oct) has no Priority column: the error lists its 7 columns",
              res.get("columns"), fx.MASTER_PIPELINE_HEADERS)
        cols = await call(w, "sheet_columns", {"tab": "master pipeline"})
        check("T15 ...sheet_columns finds it by name, any case", (cols["tab"], cols["column_count"]),
              ("Master Pipeline", 7))
        res = await call(w, "sheet_query", {"tab": "Master Pipeline", "where": [{"column": "Geography", "op": "equals",
                                                                                "value": "india"}], "limit": 200})
        check("T15 ...and it is queryable: default columns are Company and the next three mapped roles",
              (res["columns"][:3], len(res["columns"]), res["match_count"] > 0),
              (["Company (company)", "Industry (industry)", "Geography (geography)"], 4, True))
    mp = list(canned_sheet.PIPELINE_HEADERS) + ["Priority"]
    mgrid = [mp] + [r + [p] for r, p in zip(canned_sheet.pipeline_values()[1:], ["P1", "P2", "P1"])]
    with sheet_world(master=mgrid) as w:
        res = await call(w, "sheet_query", {"tab": "Master Pipeline", "where": [{"column": "Priority", "op": "equals",
                                                                                "value": "P1"}]})
        check("T15 a Master Pipeline WITH a Priority column (made up): where Priority equals P1 -> the P1 companies",
              [r["Company (company)"] for r in res["rows"]], ["Acme AI", "Initech"])
        res = await call(w, "sheet_query", {"tab": "researcher_lines", "where": [{"column": "Priority", "op": "in",
                                                                                 "value": ["p1", "p2"]}]})
        check("T15 ...by its kind name too", res["match_count"], 3)

    with sheet_world() as w:
        saved = gtm_sheet.SHEETS.__dict__.get("discovered_tabs")
        gtm_sheet.SHEETS.discovered_tabs = lambda *a, **k: [
            {"title": "Outreach PoCs", "kind": gtm_sheet.POCS},
            {"title": "Activation Score - Warmed Up Co", "kind": ""}]
        try:
            res = await call(w, "sheet_query", {"tab": "Activation Score - Warmed Up Co"})
        finally:
            if saved is None:
                del gtm_sheet.SHEETS.discovered_tabs
            else:
                gtm_sheet.SHEETS.discovered_tabs = saved
        check("E6 a tab that is not readable: the same error as today, naming the tab",
              (res.get("available"), res.get("error")),
              (False, "the GTM Playbook has no recognised Activation Score - Warmed Up Co tab"))
        res = await call(w, "sheet_query", {"tab": "Board Minutes"})
        check("E6 ...a tab that does not exist: the same, with the readable tabs listed",
              (res.get("error"), sorted(res.get("tabs") or [])),
              ("the GTM Playbook has no recognised Board Minutes tab", ["Master Pipeline", "Outreach PoCs"]))

        def down(*a, **k):
            raise gtm_sheet.SheetAccessError("the sheet is not shared with the bot", remedy="Share it.")
        w.sheet.read = down
        gtm_sheet.SHEETS.read = down
        res = await call(w, "sheet_query", {"where": [RESEARCH]})
        today = await call(w, "lookup_company", {"company": "Acme"})
        check("E6 the sheet cannot be read at all: the same error lookup_company gives",
              (res.get("available"), res.get("error"), res.get("fix")),
              (today.get("available"), today.get("error"), today.get("fix")))


# -- T16, T17 ------------------------------------------------------------------------------------------------------

def _run(args, timeout=900) -> tuple:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True, timeout=timeout,
                       env=env, encoding="utf-8", errors="replace")
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    return p.returncode, (lines[-1] if lines else (p.stderr or "").strip()[-200:])


def settings(check, *, full=False) -> None:
    check("T16 .env.example is unchanged from main (no new variables)",
          _git("diff", "--stat", "main", "--", ".env.example").strip(), "")
    code, last = _run(["-m", "pytest", "-q", "tests/test_env_example.py"])
    check("T16 tests/test_env_example.py passes", (code, "passed" in last and "failed" not in last), (0, True))
    for mod in ("sheetquery", "toolsets"):
        code, last = _run(["-m", mod])
        check(f"T17 python -m {mod}: ALL PASSED", (code, last), (0, "ALL PASSED"))
    if full:
        # verify_poc_lookup.py is LIVE (real model calls and real web searches, a few cents); --full asks for it.
        for script in ("verify_profile_lookup.py", "verify_poc_lookup.py"):
            code, last = _run([script], timeout=3600)
            check(f"T17 {script}: ALL PASSED", (code, "ALL PASSED" in last), (0, True))
        code, last = _run(["-m", "pytest", "-q", "tests/"], timeout=7200)
        check("T17 pytest -q tests/", (code, "failed" not in last), (0, True))


GROUPS = (columns_and_queries, engine, read_only, tracker_filters, tabs_and_columns)


async def run(check, *, full=False) -> None:
    for group in GROUPS:
        print(f"\n{group.__name__}")
        await group(check)
    print("\nsettings")
    settings(check, full=full)
