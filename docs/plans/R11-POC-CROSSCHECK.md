# R11 — Outreach PoCs cross-check, the agreed messages, and the second gate

9 Oct 2026. Base `cd05fa2`. One session (the agent team was stopped by the human, who asked for the work to be done
solo). Report: `docs/test-reports/R11-POC-CROSSCHECK.md`. Nothing is committed.

## 1. What was already there (confirmed, left alone)

| Brief | In the tree |
|---|---|
| R11 `weekdays: [wed]`, `max_items_per_post: 10`, first on Wednesday | `bot_rules.yaml` (R11 block and `daily_order.wed: [R11, R1, R3]`). Unchanged |
| Gate 1: `_open_poc_lookup_proposal` opens `poc_lookup`, writes nothing | `bot.py`. Kept; its payload now also carries each company's branch |
| First yes: `_apply_poc_lookup` narrows on a named company | Kept, including the narrowing; what it posts and what it opens are new |
| `db.pipeline_snapshot`, taken by the caller | Unchanged. `nextaction` is still handed data and reads nothing |
| Once-only by arithmetic (1 working day, 7-day window, Wednesdays) | Unchanged. No ledger added |
| No overflow to another week | None added |

## 2. Three things in the brief that are not what the tree says

1. **`approvals.read_vote("yes to 1 and 2 but not 3")` is YES, not NO.** `_NO_WORDS` has "no", "nope", "not yet",
   "dont", "wait", "stop" and so on, and not the bare word "not". With nobody named, the row-add path would then
   add every suggested person, including the one ruled out. `read_vote` is left alone as instructed. Instead the
   R11 path refuses it: a yes that carries a number or "not / except / only / but / without / skip" and names
   nobody writes nothing, says so, and leaves the same question open on the same message (`bot._QUALIFIED_YES_RE`,
   `wording.POC_ADD_ALL_OR_NONE`). **This is new reply handling the brief said not to add; it is the smallest thing
   that makes "nothing is written" true. Say if you would rather have it removed.**
2. **The stale `new_pipeline_company` reference is in `verify_websearch.py`, not in `websearch.RULE_QUERIES`.**
   `RULE_QUERIES` has no such key (that is the KeyError). The script still dry-ran R11 as a web rule; that block is
   removed. `websearch.py`'s tables are untouched and R11 stays out of `WEB_DEPENDENT`.
3. **The paper column's role is `paper_links`**, not `research_paper_link`. `POC_MANDATORY_FIELDS` accepts either
   spelling.

Also: **`gtm_sheet` already matches "Based" by prefix.** "Based (Oct 2026)", "Based" and "Based in (2027)" all
resolve to the `based` role today. Nothing was changed there.

## 3. The design

- **`poc_crosscheck.py` (new, pure).** `company_key`, `mandatory_roles`, `build_index`, `classify`, `collapse`, and
  the five message shapes (`m1_lines`, `render_found` for M2 / M2b / M4 in one message, `render_written` for M3).
- **The index is built by the caller** (`bot._run_next_actions`: `poc_crosscheck.build_index(tab.rows)`, every row
  of the tab) and passed as `nextaction.run(poc_company_index=...)`. It holds a row count and the mandatory roles
  left blank, per company key; never a row.
- **`nextaction._r_new_pipeline_company`**: collapses duplicates, keeps the working-day and window gates exactly as
  they were, classifies, drops complete companies, and puts `branch`, `window_start` and `sheet_order` on each item.
- **M1 is fixed text.** R11 joins `drip.VERBATIM_TYPES`: the shape was agreed line by line and ends on an offer a
  "yes" answers, so a composer must not touch it. Its bold heading is removed (`HEADINGS["R11"] = ""`). Companies
  are listed in Master Pipeline order. The tags line that opens every proactive post is kept above it.
- **The first yes** re-reads Outreach PoCs and re-classifies (Wednesday's answer may be days old), then:
  - Branch A: `_find_people_data` (the existing search, now returning data), at most 3 people; Industry from the
    company's Master Pipeline row; Email from the existing `_email_lookup`; Based and the paper link only when the
    search itself showed them (two optional fields added to the PERSON line and verified in `parse_people`).
  - Branch B: the same search, matched by name to the rows that have a mandatory gap (at most 5); only blank cells
    are shown, under the tab's own header text.
  - One message. If it suggests people, it is posted by `_offer_poc_add` (now accepting a caller's own body) and
    IS the `row_add` proposal.
- **The second yes** is the existing `_apply_poc_row_add` → `_write_poc_row`, widened to industry, designation,
  based and `paper_links` (and email only when `EMAIL_WRITE_ALLOWED`). `append_row`'s band check
  (`config.may_write_new_row_column`), duplicate check, serial and Name-cell signature are untouched. For a
  proposal whose trigger is R11 the reply is M3.
- **`websearch.render_people` is unchanged**: the `find_people` tool still uses it. R11 has its own renderer.

## 4. Gate 2 narrowing

`_apply_poc_row_add` narrows on a PERSON's name in the reply ("yes for Ross"), not on a company. That works for
R11's list as it is. Narrowing by company at gate 2 does not exist and was not added.

## 5. What a search can and cannot fill today

| Field | Source | When it is blank |
|---|---|---|
| Name, Designation, LinkedIn URL, Source | the people search (existing) | the person is not suggested without a name; the others are omitted |
| Industry | the Master Pipeline row's Industry cell | omitted when that cell is blank |
| Email | the existing `_email_lookup` (one search per person; the address must appear in a result snippet) | omitted |
| Based, Paper | NEW: two optional fields the people search may return, kept only when found in the results | omitted |

## 6. Files

New: `poc_crosscheck.py`, `verify_poc_crosscheck.py`, this plan, the report. Changed: `bot.py`, `nextaction.py`,
`drip.py`, `websearch.py`, `wording.py`, `config.py`, `.env.example`, `verify_websearch.py`, `README.md`,
`DEPLOY.md`, `sales_policy.md`, and the tests that pinned the old R11 post.

Not touched: `approvals.read_vote`, `RESTRICTED_COLUMN_RANGES`, `NEW_ROW_WRITABLE_RANGES`, `EMAIL_WRITE_ALLOWED`,
the two `NEW_COMPANY_*` defaults, `max_items_per_post`, `known[:120]` / `known_companies[:200]`,
`events_discovery._name_tokens`, Rule 13, `gtm_sheet.py`, `bot_rules.yaml`.

## 7. Questions for the human

1. The qualified-yes guard (section 2.1): keep it, or remove it and accept that "yes to 1 and 2 but not 3" adds all?
2. The tags line still opens the Wednesday post, above the agreed M1 text. Remove it for R11?
3. R11's `sheet_wording` in `bot_rules.yaml` does not mention the cross-check. I left it (the sheet's words are
   Vaishnavi's); say if it should.
4. Each Branch A person costs one extra search for the email; each Branch B row with a blank email costs one too.
   With 10 companies that can be 30 or more searches on one yes. Is that acceptable against the daily budget?
5. `verify_poc_lookup.py` and `verify_layouts.py` were on the list to run. They make live searches and live model
   calls and have no offline mode, so they were not run.
