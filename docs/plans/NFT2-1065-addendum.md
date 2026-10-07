# NFT2-1065 — addendum (the human's decisions, 7 Oct 2026)

**This file wins over `docs/plans/NFT2-1065.md` wherever they differ.** Everything the
main plan says that is not changed here still holds. Nobody commits; the human commits.
Nobody but the lead touches `CLAUDE.md`. `bot.py` is also being edited for NFT2-1064:
builder-1065 makes small targeted edits, never a whole-function rewrite or a reformat.

## A0. What the human decided

| Main-plan question | Decision |
|---|---|
| Q1 new-row write | YES. An approved add writes a NEW Outreach PoCs row: Name, Company, and the LinkedIn URL only if a search returned a `linkedin.com/in` link. A–I and S–X of an EXISTING row are never edited. Every row the bot adds is SIGNED (A1). |
| Q2 wording | Two people: `Want me to add Janajit Bagchi and Suryansh Shukla to Outreach PoCs? I'll only add them once one of you says yes.` One person: `Want me to add Janajit Bagchi to Outreach PoCs? I'll only add them once one of you says yes.` Vaishnavi confirms before deploy. |
| Search limit | Cheap first: 4 searches / 7 rounds, ONE logged extension to 6 / 9 (A2). Replaces the main plan's 6 / 9 defaults. |
| Real sheet in test mode | Accepted: an approved add writes the real sheet in test mode too. |
| Approvers | The human adds Kushal to `SALES_APPROVER_IDS` and puts Sid's id in `SALES_FINAL_SAY_ID` in both `.env` files. Agents do nothing. Tests must not assume who is an approver from the real config: they set approver ids themselves. |
| Tests | Item 13 (A3): stub the live source check everywhere a prompt is built, and make a real Sheets or Drive call during a test fail loudly. Then re-run everything. |

**Order of work.** A3 first (tester), because it is the likely way to explain the
intermittent T17 failure. `bot.POC_ROW_ADD_WRITE_WIRED` stays `False` until that failure is
explained AND fixed, the planner has reviewed the cause, and A1 is built. Then the builder
flips it and makes the `sales_policy.md` edit (A1.6).

---

## A1. The signed new row

### A1.1 Feasibility (checked)

A cell note on the Name cell is feasible with what the bot already has:
- `gspread` 6.2.1 is installed (`requirements.txt`: `gspread>=6.0.0`) and `Worksheet` has
  `insert_note(cell, content)`, `get_note(cell)`, `clear_note(cell)`.
- A note is written through the Sheets API `spreadsheets.batchUpdate`, which the existing
  scope covers: `gtm_sheet.SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]`
  (`gtm_sheet.py:127`). No new scope, no new credential, no Drive call.
- Nothing in the repo writes a note today (grep `insert_note`: no hits), so this is the first.
- A note changes no cell value and adds no column.

### A1.2 Where it is written: ONE atomic request inside `append_row` (revised by the lead, 7 Oct)

**The row and its note are written in ONE Sheets `spreadsheets.batchUpdate`, so an unsigned
row can never exist and nothing ever has to be removed.** The earlier "retry, then remove
the row" design is withdrawn: nothing in this ticket deletes or clears a row because a note
failed.

Feasibility, checked against the installed gspread 6.2.1:
- `Spreadsheet.batch_update(body)` sends `{"requests": [...]}` in one call; the Sheets API
  applies a batchUpdate all-or-nothing (if any request fails, none is applied).
- gspread's own `Worksheet.update_notes` already builds an `updateCells` request with
  `"fields": "note"` on `a1_range_to_grid_range(cell, self.id)`. The same request shape
  carries a value: `"fields": "userEnteredValue,note"` with
  `{"userEnteredValue": {...}, "note": "..."}`.
- `append_row` already knows the target row (`first_empty_row`, then the re-read
  empty-row check, `gtm_sheet.py:2610-2634`), so a ranged `updateCells` per cell fits it
  exactly; `appendCells` is NOT used (it picks its own row and would bypass that check).

`append_row` gains two keyword arguments, default off:

```python
def append_row(self, tab, values, *, reason, expect_company="", dry_run=False,
               note_role: str = "", note_text: str = "") -> dict:
```

- **Without them** (the events caller, the existing tests): the code path is exactly today's
  `ws.batch_update([...], value_input_option="USER_ENTERED")`. Unchanged.
- **With both**: step 5 (WRITE) is instead ONE
  `sh.batch_update({"requests": [<one updateCells per cell>]})`:
  ```python
  {"updateCells": {
      "range": a1_range_to_grid_range(f"{c['column']}{target}", ws.id),
      "fields": "userEnteredValue,note" if c["role"] == note_role else "userEnteredValue",
      "rows": [{"values": [cell_data]}]}}
  ```
  `cell_data["userEnteredValue"]` is `{"numberValue": int(text)}` when the text is all
  digits (the serial number), else `{"stringValue": text}` (a name, a company or a url is
  written as literal text, never parsed as a formula). The `note_role` cell also carries
  `"note": note_text`.
- `note_role` not among the cells to be written (no such column, or the name is blank) →
  refuse BEFORE any write: `error = "I cannot sign the row (no Name cell to put my note on), so I have not written anything"`.
- Step 6 (READ BACK) is unchanged for the values, and additionally reads
  `ws.get_note(note_cell)`:
  - values mismatch → the existing path, unchanged (it clears the cells it just wrote and
    reports), plus a best-effort `ws.clear_note(note_cell)` so no signature is left on an
    empty row;
  - values fine but the note does not read back (not expected after an atomic write, or the
    `get_note` call itself failed) → **keep and flag**: `ok=True`, `signed=False`,
    `note_error=<why>`. The row STAYS. Nothing is cleared or deleted.
  - both fine → `signed=True`, `note_cell=<A1>`.
- The 403 / permission handling of the existing write `except` applies to the new call too.
- `dry_run`: nothing is written; `out["would_sign"] = note_cell`.

The docstring's write step says: with a note, values and note go in one request, and WHY
(a signed row or no row; never an unsigned one, and never a delete).

Log lines (exact):
- `[gtm.append] wrote, signed and verified %d cell(s) into row %d of %r: note on %s (%s)`
- `[gtm.append] ROW %d OF %r IS UNSIGNED: the row was written but its note did not read back (%s). Left in place for a human.` (ERROR)

### A1.3 The note text (one constant, `approvals.ROW_SIGNATURE`, and `approvals.row_signature(...)`)

```
Added by Saley · approved by {approver} · {date} IST
Approval: {approval_link}
LinkedIn link found by web search: {linkedin_url}
```
- "Saley" is `config.COS_NAME`. `{approver}` is `decided_by` (the display name the vote flow already has). `{date}` is `dl.real_today_ist()` as `7 Oct 2026` (a write is real, so the real date).
- Line 3 is present only when the row carries a LinkedIn URL.
- **"The source link" is ambiguous**, so the note carries both readings and nothing is guessed:
  line 2 is the Discord link to the approver's "yes" message (`message.jump_url`; when the
  object has none, `https://discord.com/channels/{guild_id}/{channel_id}/{message_id}` built
  from the real ids; if those are missing too, the add is refused: "I could not link the
  approval, so I have not added anything"). Line 3 is the search-result URL the LinkedIn
  link came from, which IS the `linkedin.com/in` URL the search returned. If the human wants
  only one of the two, it is a one-line change in the constant. (Raised to the lead.)

`bot._write_poc_row` passes `note_role="name"`, `note_text=approvals.row_signature(...)`.
It is the ONLY caller that passes them.

### A1.4 The reply after an approver's yes (`bot._apply_poc_row_add`)

- signed: `Added Janajit Bagchi (ARTPARK India) to Outreach PoCs at row N, with my note on the Name cell.`
- dry run: the existing dry-run suffix, plus nothing about a note.
- write refused or failed: `Did not add Janajit Bagchi: <error>` — and because the write is one request, nothing is on the sheet.
- row written, note not confirmed (keep and flag): `Added Janajit Bagchi (ARTPARK India) to Outreach PoCs at row N, but I could NOT confirm my 'Added by Saley' note on the Name cell — the row is unsigned and needs a human to check it.` Audit `poc_row_unsigned`.
- `state.audit("poc_row_added", …)` gains `signed`, `note_cell`, `approval_link`.

### A1.5 EXACTLY which cells an approved add writes

Columns are found by HEADER at run time (`tab.canonical_role_to_col`), never by a fixed
letter; the letters below are the tab's real layout as pinned in `tests/conftest.py:19-26`.

| Cell on the new row | Header | Value | Who puts it there |
|---|---|---|---|
| A | Sr No | the highest existing serial + 1 | `append_row` itself: `payload["sr_no"] = self._next_sr_no(tab)` when the tab has that column and the caller gave none (`gtm_sheet.py:2569-2572`; `_next_sr_no`, `gtm_sheet.py:2464-2480`). Existing behaviour for every appended row, events included |
| B | Company/Uni | the company, as the asker or the search gave it | `bot._write_poc_row` (`bot.py:7212-7214`) |
| D | Name | the person's name | `bot._write_poc_row` |
| D (note, not a value) | — | the signature (A1.3) | the same request |
| I | LI Url | the `linkedin.com/in/…` URL, only if a search returned it this turn; otherwise the cell is not written at all | `bot._write_poc_row` |

Nothing else. `append_row` skips every empty value (`gtm_sheet.py:2574-2577`) and fills no
date added, owner, status, industry, designation, email or default of any kind; C, E–H and
J–X of the new row stay blank.

**The one cell beyond the human's three is the serial number in A.** It is not a choice
made in this ticket: `append_row` adds it to every row it appends, so that the new row is
numbered like the rows above it (max + 1, never a repeat). The builder adds NO cell beyond
these and does not change the serial behaviour either way until the human answers; if the
human says "no serial", the change is a `fill_serial=False` keyword on `append_row`
(default True, so the events caller is unchanged), passed by `_write_poc_row`.

Nothing here can reach an existing row: the write goes only into the re-read empty row, and
the note goes on that row's Name cell. No new column, ever, without asking the human. No
signature anywhere but the note.

### A1.6 Text corrections (builder, when the constant is flipped)

- `sales_policy.md` hard limit 5: replace "Never a whole row, never a column outside that window." with
  "On an existing row, never a column outside that window. A NEW row on Outreach PoCs (Name, Company, and a LinkedIn URL a search returned) is added only after you ask and an approver says yes, and it carries your note on the Name cell: who approved it and when."
- `sales_strategy.md` §10 Never, the A–I bullet: unchanged (it already says "of an existing Outreach PoCs row"). §8, append to the main plan's added sentence: " Every row Saley adds carries a note on the Name cell saying Saley added it, who approved it and when."
- `docs/SOURCES.md` GTM row: add "a new Outreach PoCs row (Name, Company, LinkedIn URL) after an approver's yes, signed with a cell note".
- `README.md` "Row additions" and "Permission before every write": the signature, written in one request with the row; the keep-and-flag reply; the new wording.
- `CLAUDE.md`: already edited by the lead. Nobody else touches it.

### A1.7 The offer wording (`approvals.ROW_ADD_OFFER`)

```python
ROW_ADD_OFFER = "Want me to add {names} to {tab}? I'll only add them once one of you says yes."
```
"them" for one person too (the human's wording). The `{rows}` placeholder goes.
`approvals.row_add_question` is unchanged.

---

## A2. Search limit: cheap first, one extension

### A2.1 Variables (all four in `.env.example`, uncommented, with these defaults)

| Variable | Default | |
|---|---|---|
| `WEB_QUESTION_MAX_SEARCHES` | 4 | CHANGED default (HEAD 2; the main plan's 6 is withdrawn) |
| `QUERY_ENGINE_MAX_TOOL_ITERATIONS` | 7 | CHANGED default (HEAD 5; the main plan's 9 is withdrawn) |
| `WEB_QUESTION_EXTENDED_SEARCHES` | 6 | NEW |
| `QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS` | 9 | NEW |

`config.py`: the extended value is never below its base (`max(base, extended)`); setting
extended equal to base turns the extension off. Startup: WARNING when rounds < searches + 3
for either pair; the INFO line becomes
`[config] one question: up to %d web search(es) and %d tool round(s); one extension to %d and %d.`

### A2.2 `query_engine.QuestionLimits` (new, small, pure)

```python
class QuestionLimits:
    """One question's two caps, and its ONE extension."""
    def __init__(self, *, searches, rounds, ext_searches, ext_rounds, label=""): ...
    searches: int        # current search cap
    rounds: int          # current tool-round cap
    extended: bool       # False until extend() succeeds; never reset
    def extend(self, *, hit: str, detail: str = "") -> bool
```
`extend` returns False if `extended` is already True or if neither extended value is above
its base. Otherwise it sets `searches = ext_searches`, `rounds = ext_rounds`,
`extended = True`, writes THE one log line, and returns True:

```
[engine] %s LIMIT EXTENDED ONCE: searches %d -> %d, tool rounds %d -> %d (hit the %s limit; still unchecked: %r)
```
`%s` first is `label` (`msg=<id>`); `hit` is `"search"` or `"round"`. It cannot fire twice
for one question because the object is created once per question and `extended` is never reset.

### A2.3 "Named people or items still unchecked", in code

No model call decides it. Two triggers, one shared flag:

1. **Search limit** (`bot._web_question_tools._search`): a `web_search` call arrives when
   `out["asked"] >= limits.searches`, AND its query, normalised (lower-case, `[a-z0-9]+`
   joined by spaces), is non-empty and is NOT one already in `out["queries"]`. A new,
   different query at the limit IS an item the model has not checked. Then
   `limits.extend(hit="search", detail=query)`; on True the search runs. A repeated query
   never extends and gets the limit result.
2. **Round limit** (`query_engine.answer`): the loop reaches `limits.rounds` and the turn it
   just processed called `web_search`. Then `limits.extend(hit="round", detail=<that turn's
   last web_search query>)`; on True the loop continues to the new cap.

After the extension, or when it is refused, the limit result is unchanged from the build
(`error`, `searches_run`, `not_run`, `say`), so the per-person "not checked yet" lines still
come out when even the extension runs out. `say` still ends "Do not mention a limit."

### A2.4 Wiring (small edits)

- `bot._answer_with_engine`: build one `QuestionLimits` from the four config values with
  `label=f"msg={message.id}"`; put it in `web_out["limits"]` before `_websearch_tools`;
  pass `limits=` to `query_engine.answer`.
- `bot._web_question_tools`: `limit` is read from `out["limits"].searches` at each call
  (not captured once); the error text names the current cap.
- `query_engine.answer(..., limits: Optional[QuestionLimits] = None)`: the `for i in
  range(MAX_TOOL_ITERATIONS)` loop becomes a `while i < cap` loop with
  `cap = limits.rounds if limits else MAX_TOOL_ITERATIONS`; trigger 2 at the cap;
  `_trim_old_results(..., iteration=cap + 1)`; the iteration log line prints the live cap.
  With `limits=None` behaviour is exactly as now (every other caller and `verify_llm_audit`).
- `outcome["limit_extended"] = limits.extended` for the tests.
- The daily budgets (`SEARCH_DAILY_BUDGET`, `TOKEN_DAILY_BUDGET`) still gate everything; the
  extension cannot pass them.
- Server-side backend (`SEARCH_BACKEND=anthropic`): no client search tool, so only the round
  trigger exists; nothing else changes.

### A2.5 Env lines for the human (laptop `.env` AND `/opt/sales-bot/.env`)

Both machines SET the first two to old values today, so the defaults alone change nothing:
```
WEB_QUESTION_MAX_SEARCHES=4
QUERY_ENGINE_MAX_TOOL_ITERATIONS=7
WEB_QUESTION_EXTENDED_SEARCHES=6
QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9
```
NEW: `WEB_QUESTION_EXTENDED_SEARCHES`, `QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS`.
CHANGED default: `WEB_QUESTION_MAX_SEARCHES` 2 → 4, `QUERY_ENGINE_MAX_TOOL_ITERATIONS` 5 → 7.
RETIRED: none. (Plus the human's own edits to `SALES_APPROVER_IDS` and `SALES_FINAL_SAY_ID`.)

### A2.6 Cost: measured and reported at BOTH levels

The tester's COST section prints, from the requests the engine really builds, three rows:
old (2 / 5), base (4 / 7, no extension), extended (6 / 9, extension fired): model calls,
searches, request tokens, budget tokens (cache reads at 10%), dollars, and the two deltas
(old → base, base → extended). The report states them; README section "Search and news"
quotes them. The 7 Oct measurement for 6 / 9 against 2 / 5 was +$0.068 and +20,320 budget
tokens; the base level must come in below that.

---

## A3. Item 13: no test touches a live source

`tests/offline_guard.py` (new, tester):
- `install()`:
  - replaces `gtm_sheet.GTMSheets._get_client`, `mapping_sheet.MappingSheet._get_client`
    and the credential loader in `drive.py` (the function around `drive.py:174` that calls
    `Credentials.from_service_account_file`) with a function that appends to
    `offline_guard.VIOLATIONS` (what was called, and a short stack) and raises
    `offline_guard.LiveSourceCall`;
  - replaces `sources.status_report` with a fixed list (every source `connected`, fixed
    detail text), so every prompt built in a test is byte-stable and probes nothing.
    A test that is ABOUT the statuses passes its own list to `install(statuses=...)`.
- `assert_clean()`: fails, printing every violation, if `VIOLATIONS` is non-empty. Needed
  because most call sites catch `Exception` and degrade quietly; the raise alone is not loud.
- `tests/conftest.py`: an autouse fixture installs it and asserts clean at teardown.
- Every offline `verify_*.py` that builds a prompt (`verify_profile_lookup`,
  `verify_replay_oct6`, `verify_notes_scope`, `verify_llm_audit`, `verify_news_feed`,
  `verify_news_question`, `verify_reminders`, `verify_answer_voice`, `verify_interim`,
  `verify_tokens` offline, and any other that reaches `persona.system_blocks` /
  `system_preamble` / `QueryEngine.answer`): `install()` right after `import config`, and
  `check("no live Sheets or Drive call was made", offline_guard.VIOLATIONS, [])` before the
  final line. A script that stubs its own tab installs the guard first and its stub second.
- Exempt, and still not run by anyone: `verify_simulation.py`, `verify_parity.py` without
  `--fixture-only`, `verify_poc_lookup.py`, `verify_layouts.py`, and the `--live` flags.
- Then re-run everything and compare against the known-red baseline. A script that turns red
  because it WAS reading a live source is a finding: report it, fix the script's stubbing,
  never weaken the guard.

**The intermittent T17 failure.** Symptom: test mode and live disagree on a refusal, with the
proposal `open` in one run and `applied` in the other. Not explained yet. Things to check,
in this order: (1) with the guard installed, does a violation appear (a live tab read whose
cache or timing differs between the two runs); (2) is anything shared between the two runs
of `both()` (the DB file, the stub tab's `rows`, `sink`, `FakeMessage` ids, so that the
second run sees the first run's open proposal through `db.open_proposals_of_kind` or
`latest_open_proposal`); (3) the vote path: `guessed and ref` with a `message_id` that is
set asynchronously by `set_proposal_message`. The tester reports the cause to the planner
before the builder flips the constant.

---

## A4. Tests added or changed (on top of the main plan's T-table)

| ID | Case | Assert |
|---|---|---|
| A-T1 | Offer wording | `approvals.row_add_offer` gives the two A0 sentences exactly, for two people and for one |
| A-T2 | Signed row | approver's yes → `append_row` called with `note_role="name"` and the A1.3 text: line 1 names the approver and the real IST date, line 2 is the approval link, line 3 only when a URL is present; the reply says "with my note on the Name cell" |
| A-T3 | `append_row` note (fake worksheet, `tests/`) | the note is on the Name cell of the new row only; `get_note` is checked; digits go as `numberValue`, everything else as `stringValue` (a value starting with `=` stays text); without `note_role` the call is the old `ws.batch_update(..., USER_ENTERED)` and no note request is made (events path unchanged) |
| A-T4 | Atomic write | on the signed path exactly ONE `sh.batch_update` call carries every value AND the note (the Name cell's request has `fields` `userEnteredValue,note`), and `ws.batch_update` is not called; the batch raising → `ok=False`, nothing on the fake sheet, reply `Did not add …`; the note not reading back → row stays, `signed=False`, the "unsigned and needs a human" reply, audit `poc_row_unsigned`; no code path clears or deletes a row because of a note; no Name column → refused before any write |
| A-T5 | No approval link | add refused, nothing written |
| A-T6 | Existing rows | after any add, no cell and no note outside the appended row was touched; A–I and S–X of existing rows identical before and after |
| A-T7 | Dry run | no note written, reply says dry run |
| A-T8 | Base limit | defaults are 4 / 7 / 6 / 9; four searches run with no extension and no extension log line |
| A-T9 | Extension by searches | a 5th DIFFERENT query → exactly one `LIMIT EXTENDED ONCE` line, searches 5 and 6 run, the 7th gets the limit result with `searches_run` (six) and `not_run`; the reply has "not checked yet" for the rest and no "limit" |
| A-T10 | No extension for a repeat | a 5th query equal to an earlier one → limit result, no log line, `extended` False |
| A-T11 | Extension by rounds | the 7th round's turn called `web_search` → extended to 9, once; a turn that called only other tools → no extension |
| A-T12 | Once only | both triggers hit in one question → one log line; a second question gets a fresh extension |
| A-T13 | Extension off | extended == base → never extends |
| A-T14 | Config | the WARNING for each pair; `tests/test_env_example.py` green with the two new keys |
| A-T15 | Guard | a deliberate `gtm_sheet.SHEETS._get_client()` in a test is recorded and raises; `sources.status_report` is the fixed list; every offline script ends with zero violations |
| A-T16 | Parity | A-T2, A-T4, A-T9 with `SALES_TEST_MODE` off and on: identical |

Main-plan tests that change: T8 and T19 use 4 / 7 / 6 / 9; T11 asserts the new wording via
`approvals.row_add_offer`; T13 and T17 stop being "pending Q1" once the constant is flipped
and gain the note assertions; the COST section prints three levels (A2.6). Replay step 9
ends with the new wording; step 5 runs at the base limit and shows the extension once.

Report (`docs/test-reports/NFT2-1065.md`) additions: the T17 cause and fix; the guard's
result per script; the three-level cost table; the four `.env` lines; the live checklist
updated (wording, "check the Name cell's note on the new row: Added by Saley · approved by
… · date, the approval link", "ask about six people: expect one extension in the log and
'not checked yet' beyond it").

## A5. What still must not change

Everything in the main plan's section 5, with two amendments: `gtm_sheet.append_row` gains
the optional signed-write path and nothing else (its six checks and its existing callers are
untouched; no path deletes a row); `CLAUDE.md` is the lead's. `today` stays exclusive. LinkedIn is never fetched.
No new column. No commit, no push.
