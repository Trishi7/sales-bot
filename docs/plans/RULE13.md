# RULE 13 — Next Steps follow-ups, and the 7 Oct sheet layout

Planner: planner-r13 · written 2026-10-07 · tree: HEAD 7ff8eb0 on main, clean.
Line numbers are the tree at that commit. Builder and tester work from this file alone.
There is no Linear ticket: the rule text and Vaishnavi's answers are in the lead's spawn
prompt; the parts a builder or tester needs are restated here.

Scope, decided by the human: build the 7 Oct sheet layout first (section 4), then Rule 13
on the EXISTING pinned-time path (sections 5 to 9). The reply handling waits for NFT2-1063
(section 14). The priority/fixed_time conversion waits for NFT2-1069 (section 15).

---

## 0. Awaiting Vaishnavi (built as defaults; each changes without a code change)

| # | Default built | Where it is changed |
|---|---|---|
| a | Weekdays only, 15:00 IST. Saturday and Sunday silent. | `bot_rules.yaml` R13 `weekdays`; `.env` `NEXT_STEP_TIME` |
| b | Outside the daily cap. | `bot_rules.yaml` R13 `counts_toward_cap` (R13 is NOT in `drip.NEVER_COUNTED`, so the file decides) |
| c | Remind only. Saley never writes Q to W; it asks people to update the cells. | `.env` `RESTRICTED_COLUMN_RANGES` holds the lock. There is no switch that makes Rule 13 write. |
| d | "VR" is Vaishnavi; a "done" from any teammate counts. | Nothing built yet (replies are deferred, section 14). |
| e | Sheet order. Priority (AF) is read and shown in the preview, never used for order. | Code constant today; a change of order is a code change and is listed as Q6. |
| f | LI DM Sent = "Replied": no call chase; the line asks whether a meeting is being set up. | `.env` `NEXT_STEP_DM_REPLIED_MARKERS` (empty = treat like any DM) |
| g | Meeting dated today or later, not Completed: Rule 13 pauses for that person. Past date, not Completed: Rule 13 carries on. Completed: Rule 13 ends for them (R9 takes over). | `.env` `NEXT_STEP_PAUSE_FOR_BOOKED_MEETING` (true/false) |

Every number in the timing table is an `.env` variable (section 5.3).

---

## 1. Real configuration (.env.agent, regenerated 2026-10-07T17:44 from this laptop's .env)

I re-ran `python tools/redact_env.py` because the 14:51 copy disagreed with the brief. The
fresh copy disagrees too. The server's `.env` is not visible from here.

| Variable | Laptop today | Code default today | Matters because |
|---|---|---|---|
| RESTRICTED_COLUMN_RANGES | `A:I,Q:W,Z:AE` | `A:I,S:X` (config.py:1594-1596) | already the target |
| NEW_ROW_WRITABLE_RANGES | **`A:R`** | `A:R` (config.py:984-986) | **NOT `A:P,X:Y` as the brief says.** With the 7 Oct layout a new row may fill Q (the dropdown) and R (1st Email Sent). `row_add` does not use them, so nothing wrong has been written, but the fence is open. |
| GTM_COLUMN_MAP | **empty** | `{}` | **The stop-gap is NOT set on this laptop.** If the bot runs here today, `next_steps` maps to Q "Next Steps" and R9, `last_note` and `prospect_signature` read the dropdown. |
| SALES_TEST_MODE / DB_PATH / SALES_DIGEST_ENABLED | false / `./sales_bot.db` / true | | this laptop posts LIVE if the bot runs |
| COS_FOLLOWUP_CHECK_INTERVAL_MINUTES | **150** | 15 (config.py:805) | the drip sends only on a sweep tick (bot.py:5845). "15:00" means the first tick at or after 15:00: up to 14 minutes late at the default, up to 149 minutes late here. |
| MESSAGE_GAP / JITTER / GAP_MIN | 120 / 120 / 120 | 90 / 15 / 30 | `drip.min_gap_minutes()` is 1 minute here, 30 at the defaults (section 7.3) |
| DAILY_MESSAGE_CAP | 15 | 5 | R13 is outside it either way |
| SALES_DEFAULT_OWNER_ID | empty | | R13's post is addressed by the tags line only (section 8.4) |
| SALES_ALWAYS_TAG_IDS | one id (Vaishnavi) | | who the tags line names |
| EMAIL_WRITE_ALLOWED | true | false | the Email cell (F) exception is live here |
| SIMULATION_PREFIX | `[TEST-live]` | `[TEST]` | why three parity scripts are red at baseline |

---

## 2. Each lead, checked

**L1. Header mapping. CONFIRMED** (ran the real `_map_headers`, gtm_sheet.py:1610-1662, on
both header rows with `GTM_COLUMN_MAP` empty). Today's row: Q "Next Steps" maps to
`next_steps` by the exact alias "next steps" (gtm_sheet.py:495-496); R to W, Z
"Notes/Remarks" and AF "Priority" map to nothing and ride in `_extra`. Every other role
lands on its own column. Pre-7 Oct row: S "Next Steps/Notes" maps to `next_steps`.
Readers of the wrong cell today: R9 (nextaction.py:1102), `last_note` (nextaction.py:292,
used by R7 at 996), `prospect_signature` (nextaction.py:924), `_reset_answered_ladders`
(bot.py:9019), the stall clock (bot.py:9145), tracker.py:412 and 447, prep.py:350,
bot.py:3498 and 5299.
**The stop-gap part is REFUTED for this laptop**: `GTM_COLUMN_MAP` is empty (section 1).

**L2. Bands. CONFIRMED for the code, REFUTED for the laptop's new-row band.** Defaults
`A:I,S:X` (config.py:1596) and `A:R` (config.py:986). The startup warning hard-codes
`parse_column_ranges("S:X")` and says "The default is A:R" (config.py:3494-3502). The
line above it says "an EXISTING row's A:I and S:X stay locked" (config.py:3488-3490).
Old-layout text: section 4.5 lists every place.

**L3. R6. CONFIRMED.** `_r_li_no_dm` nextaction.py:927-969; the line is at 954-955. Found
beyond the brief: the words a person actually reads also say "no DM" and ask for a DM
(drip.py:941-948 `_ASK`, heading drip.py:1112, `plain` bot_rules.yaml:339). See Q3.

**L4. R7. CONFIRMED.** `_r_dm_no_meeting` nextaction.py:972-1009, Mondays
(bot_rules.yaml:355-371).

**L5. R9's ladder pattern. CONFIRMED, with one correction.** Table db.py:722-728; read
db.py:2408; advanced only by the sender after a real send (bot.py:8432-8436, 8957-8995);
reset in `_reset_answered_ladders` (bot.py:8997-9034), which is called from
`_run_next_actions` (bot.py:9372), so the preview DOES write (an idempotent delete).
Correction: a TEST DAY is not a simulation. `_live_test_day` (bot.py:11220) sends through
`_send_drip_message` with the real `self.db`; only a simulation swaps in a sandbox copy
(bot.py:11570-11588). So on this laptop a "make it Monday" advances R9's ladder in
`./sales_bot.db`. "Do what R9 does" would let a test day move Rule 13's live rotation.
Section 6.4 is stricter than R9 on that one path. See Q5.

**L6. drip. CONFIRMED.** `NEVER_HELD` drip.py:471-473; `counts` 481-497 and
`NEVER_COUNTED` 454-457; `RULE_ONLY_TYPES` 103; `pinned_time` 525-543 reads the group's
`dayof_time` (drip.py:166-168). Found beyond the brief: a `RULE_ONLY_TYPES` group is
given `config.DELIVERABLE_DEFAULT_OWNER` as its owner (drip.py:170-173), which is R4's
setting and wrong for R13 (section 8.4).

**L7. stop_reason / parse_date. CONFIRMED.** Prospect Status "Unresponsive" stops a row
for every rule (nextaction.py:245-247, `CLOSURE_STOP_MARKERS` contains it).
`deadlines.parse_date("05.10.2026")` returns 2026-10-05 (format `%d.%m.%Y`,
deadlines.py:272-294). A two-digit year with dots ("05.10.26") does NOT parse; such a row
is "no readable date" and is skipped and listed.

**L8. rules.py. CONFIRMED.** Unknown trigger is a startup error (rules.py:283-289);
`PLAIN_BY_TRIGGER` rules.py:115-128.

**Causes the brief missed**

- **F1. The one-contact-a-day dedup would starve Rule 13.** `nextaction.run` keeps the
  item from the rule listed FIRST in bot_rules.yaml (nextaction.py:1478-1503). R6 selects
  every connected contact with no DM (23 of the 29), with no limit before the dedup. With
  R13 listed after R6, every Tuesday and Friday R13 would lose almost all its people.
  Fix: R13 sits before R5 in the file (section 8.1).
- **F2. A "yes" replied to a Rule 13 post can approve an unrelated write.**
  `_maybe_vote_on_proposal` (bot.py:6312-6356) reads "yes" as a vote; when the replied-to
  message has no proposal it falls back to the newest open proposal of ANY kind, and only
  `row_add` is protected. With an R5 `email_write` offer or a cell update open, an
  approver's "yes" to a Rule 13 post writes to the sheet. "Yes" is one of the ticket's own
  expected replies. A narrow guard is built now (section 9.3).
- **F3. The catch-up guard can hold a 15:00 post.** `_maybe_send_drip` returns when any
  post went out less than `drip.min_gap_minutes()` ago (bot.py:7843-7848), before it
  plans, pinned posts included. Section 7.3.
- **F4. `write_cells_on` has no band check** (gtm_sheet.py:3311-3438) and
  `outreach_pocs` is in `SHEET_APPENDABLE_TABS`. Its only caller passes the Events tab
  (bot.py:6767-6786), so nothing reaches it today. Section 4.6 closes it.
- **F5. AF "Priority" is outside every band** (to the right of Z:AE). Once it has a role,
  only `sheetwrite.tier` (unknown role = NEVER, sheetwrite.py:164-175) stops a write. Q4.
- **F6. `.env.agent` was stale and the brief's two claims about it are wrong** (section 1).

---

## 3. What happens today, message by message

The 6 Oct fixture (tests/fixtures/oct6_exchange.md) is nine questions; none touches a
Rule 13 path. Steps 2, 3, 4, 6, 7 are NFT2-1063's and stay stubs. What Rule 13 changes
for a human message is one thing only, traced here.

**A reply "done" to a Rule 13 post, before NFT2-1063:**
1. `on_message` bot.py:882 → `should_respond` 966 → reply-to-bot 1037 → True.
2. `_handle_query` 1124 → `_route_query` 1221 → `_maybe_apply_sheet_update` 6170.
3. `_maybe_vote_on_proposal` 6312: `approvals.read_vote("done")` is "" → not a vote.
   ("yes" IS a vote: F2. After section 9.3 it is not a vote either.)
4. `_drip_context_for` 6286 finds the drip row: `companies` = the post's companies,
   `asked_about` = the trigger.
5. `_sheet_update_prefilter` 6239: "a reply to the drip message about …" → the extractor
   runs (one light model call).
6. Extractor says `none` → falls to the engine → an ordinary answer (the 6 Oct "Sure."
   shape; NFT2-1063 owns it). Extractor says `update` → `_resolve_write_row` 6535: no
   company named and several in the post → "which company" (`wording.which_company`); one
   company → `plan_writes`: a Q to W or Z to AE role is acknowledged and refused
   (sheetwrite.py:263-279); a J to P or X to Y role becomes a PROPOSAL that waits for an
   approver (bot.py:6629).
7. Nothing is written without a separate approver's yes on a proposal that names the
   cell. No Rule 13 state changes. The person may get an unhelpful answer. That is the
   honest interim behaviour and the plan says so in README.

---

## 4. The 7 Oct layout (build first)

### 4.1 Role names

| Column | Header | Role | New? |
|---|---|---|---|
| Q | Next Steps | `outreach_step` | new |
| R / S | 1st Email Sent / 1st Email Date | `email_1_sent` / `email_1_date` | new |
| T / U | 2nd Email Sent / 2nd Email Date | `email_2_sent` / `email_2_date` | new |
| V / W | 3rd Email Sent / 3rd Email Date | `email_3_sent` / `email_3_date` | new |
| Z | Notes/Remarks | `next_steps` | **kept** |
| AF | Priority | `poc_priority` | new |

**`next_steps` is NOT renamed.** It stays the notes role and now maps to Z. Reason: it
has 20-odd readers and writers, the write extractor emits the name (llm.py:250), and
`next_steps` is also the key of a meeting note's action list (notes.py:1131,
meetings.py:294 and 527, prep.py:276, evidence.py:283, db.py:637), a different thing a
rename would be confused with. Every reader, for the builder to re-read (labels only; no
logic change): nextaction.py:292, 924, 1102 · bot.py:3498, 5299, 9019, 9145 ·
tracker.py:412, 447 · prep.py:350 · research.py:267 · sheetwrite.py:108, 155, 722 ·
gtm_sheet.py:203, 219, 495, 3508 · llm.py:250. Writers: none can land (Z is restricted);
`sheetwrite.REPLY_ROLES` keeps it so the refusal names the column.
Put a comment on the role in `ROLES[POCS]`: "THE NOTES ROLE. The name is historical: until
7 Oct the column was 'Next Steps/Notes'. The dropdown is `outreach_step`."
`poc_priority`, not `priority`: an item's `priority` is its band (nextaction.py:386).

### 4.2 `gtm_sheet.ROLES[POCS]` aliases (gtm_sheet.py:421-519)

```
"outreach_step": ("next steps", "next step", "outreach step", "next action"),
"email_1_sent":  ("1st email sent", "first email sent", "email 1 sent"),
"email_1_date":  ("1st email date", "first email date", "email 1 date"),
   (2nd/second/2 and 3rd/third/3 the same)
"next_steps":    ("notes remarks", "notes/remarks", "next steps notes",
                  "next steps/notes", "notes", "remarks", "comments"),
"poc_priority":  ("priority", "poc priority", "priority level"),
```

Removed from `next_steps`: "next steps", "next step", "next action", "action".
Place the new roles in sheet order and rewrite the three block comments (4.5).

How each row resolves (`_map_headers` runs an exact pass over ALL roles, then a substring
pass, gtm_sheet.py:1643-1661):
- Today's row: Q is exact for `outreach_step`; R to W exact for the six email roles; Z
  exact for `next_steps` ("notes remarks"); AF exact for `poc_priority`. F "Email id" is
  taken exactly by `email` first, so `email`'s loose alias cannot reach R to W.
- Pre-7 Oct row: S "Next Steps/Notes" is exact for `next_steps`. `outreach_step`, the
  email roles and `poc_priority` map to nothing (no header matches, and S is taken before
  the substring pass).

**The veto (new, in `_map_headers`).** Aliases alone do not make "Q never maps to the
notes role": on a drifted header the substring pass tests `h in key`, and "next steps" is
a substring of "next steps notes". Add a module constant and check it in the override
loop and in both passes:

```
_POCS_ROLE_VETO = {
    "next_steps":    lambda h: h in ("next steps", "next step"),
    "outreach_step": lambda h: "note" in h or "remark" in h,
}
```
`h` is the normalised header. A vetoed pair is skipped; when the override names it, log a
WARNING ("GTM_COLUMN_MAP points the notes role at the Next Steps dropdown; ignored") and
fall back to detection. Applies to `kind == POCS` only.

### 4.3 Role lists
- `NEXT_ACTION_ROLES` (gtm_sheet.py:216-221): add `outreach_step`, the six email roles
  and `poc_priority`. This is the startup schema report (bot.py:~750-764).
- `CADENCE_ROLES` (199-205): do NOT add them. `_warn_colour_coded` (1893-1927) warns on
  any CADENCE role filled on 5% of rows or fewer; the email columns are nearly empty by
  design (3 rows) and would warn on every read.

### 4.4 Can GTM_COLUMN_MAP be blanked?
Yes, and it should be. After 4.2 the aliases do what the stop-gap did. The laptop's is
already blank. If the server's holds `{"outreach_pocs":{"next_steps":"Notes/Remarks"}}`
it is harmless (same column) but should be blanked so one mapping exists, not two.

### 4.5 Bands: defaults and every description of the old layout
`config.py`: `RESTRICTED_COLUMN_RANGES` default → `"A:I,Q:W,Z:AE"` (1596);
`NEW_ROW_WRITABLE_RANGES` default → `"A:P,X:Y"` (986). `.env.example` 1157 and 1982 the
same. With them `writable_windows()` is [(9,15),(23,24)] and `writable_window_label()` is
"J:P, X:Y", so `[gtm.window]` (gtm_sheet.py:1967-1978) logs J:P and X:Y with no code
change there.

Startup warning (config.py:3486-3502). Replace the literal "S:X" with the configured
bands: `locked = indexes of RESTRICTED_COLUMN_BANDS[1:]` (every band after the identity
block, which a new row is allowed to fill); `overlap = new_row_writable_indexes() &
locked`. When it overlaps, warn naming the columns that overlap as ranges and "The default
is A:P,X:Y". The INFO line above it prints the configured bands instead of "A:I and S:X".
On this laptop today (A:R against Q:W) the new warning fires and names Q:R. Also 2885:
"the default is 'A:I,Q:W,Z:AE'".

Rewrite, text only: config.py 876-878, 962-971, 981-983, 1580, 1621, 1680, 1700 ·
gtm_sheet.py 78-84, 402-408 ("COLUMNS A-AF", "thirty-two facts"), 449-450 ("J-P and X-Y:
THE WRITABLE WINDOWS"), 493-494 ("Q-W: THE STEP BLOCK" and "Z-AE: THE COMMERCIAL BLOCK"),
1933-1940, 2501-2503, 2608-2609 (the refusal says "the commercial block"; say "that
column is not one a new row may fill"), 2955, 2985-2988, 3318 · sheetwrite.py 25, 89-93,
104-107, 155 (`"next_steps": "notes / remarks"`), self-test 776-830 (move to the 7 Oct
row) · db.py 626, 2781 · sources.py:193 (prints the setting; check the sentence) ·
.env.example 505, 906-930 (the column table: 32 columns A-AF), 1115, 1128-1146,
1968-1982 · README 57, 863-889, 1127, 1207, 1411, 1545-1547, 1620, 2576-2580, 2826 ·
sales_policy.md (grep "A:I", "S:X", "J:R"; none found by the planner, re-check) ·
docs/SOURCES.md:25 · DEPLOY.md (no band text today; add the deploy step, section 11).
Also `bot._reset_answered_ladders` log text bot.py:9020 → "Notes/Remarks is filled".

### 4.6 Proof: nothing can write Q to W or Z to AE on an existing row

Every call that writes to the GTM spreadsheet is in gtm_sheet.py:
| Path | Caller | Why it cannot |
|---|---|---|
| `write_cells` 3040 | bot.py:7513 (approved cell update), `undo_cells` 3461 (bot.py:7609), CLI 3579 | each role's real column index goes through `_refuse_if_restricted` (3124-3132, `config.is_restricted_column`, fails closed) before any request |
| `sheetwrite.plan_writes` 227 | bot.py:6588 | a restricted index is turned into an "ask", never a write (263-279), before the tier |
| `write_email` 3186 | bot.py:8745, `undo_cells` 3456 | the role is the constant `EMAIL_ROLE`; the cell is `role_to_col["email"]`, which is F on both layouts; no column parameter exists |
| `append_row` 2482 | bot.py:7271 (`row_add`), 6715 (Events tab) | writes only to `first_empty_row`, re-read and refused if any cell is occupied (2645-2669); on Outreach PoCs each column must pass `may_write_new_row_column` (2603) |
| `write_cells_on` 3311 | bot.py:6780 (Events tab only) | no band check (F4). **Build:** refuse when `tab.kind == POCS` ("Outreach PoCs cells go through write_cells"), first thing after the `tab is None` check |
| `clear_cells` 2824 | none | dead code, no guard. Leave it; do not call it. |

`row_add` under `A:P,X:Y`: `_write_poc_row` (bot.py:7230-7277) sends `company`, `name`
(with the note) and `li_url`, and `append_row` adds `sr_no` when
`POC_ROW_ADD_FILL_SERIAL`. On the 7 Oct row those are B, D, I and A. All inside A:P. It
still works; nothing it writes moved.

Tier check to add as a test, not code: `sheetwrite.allowed_for(role, trigger)` is False
for `outreach_step`, the six email roles and `poc_priority`, for both triggers.

---

## 5. The rule

### 5.1 Names
Trigger `next_step_followups`; `nextaction.R_NEXT_STEPS`; evaluator
`_r_next_step_followups`; `TYPE_LABELS` "Next steps for connected contacts";
`RULE_BANDS` → `P_CHASE`. Add to `rules.KNOWN_TRIGGERS` and `PLAIN_BY_TRIGGER`
("next steps for the people we're connected with"). Not `WEB_DEPENDENT`.

### 5.2 Who is in
Rows come from `ctx["prospect_rows"]` when it is not None, else `ctx["rows"]` (the same
two lines R5 uses, nextaction.py:840-841), because a Connected row with no date may not be
"active". In sheet order (`_row`). For each row:
0. (Added in review.) If NO row carries the key `outreach_step`, the tab has no Next Steps
   column mapped (`_parse_values` sets a role's key only when the role mapped). The rule
   then names nobody: report `state: "no_step_column"`, one WARNING naming the header it
   looked for and GTM_COLUMN_MAP, return []. It must never say "Next Steps is blank" about
   a column it cannot see. A row that lacks the key while others carry it is skipped the
   same way.
0b. (Builder's deviation, accepted.) Once today's post has gone, the people the state says
   were named today (`last_date == today`) are returned as they are and nobody new is
   picked; `report["posted_today"] = True`. The queue is recomputed every tick, and
   without this the next five would be taken from R5/R6 by the dedup for a post that
   cannot be sent again.
1. `_norm(sid_li_added)` is one of `NEXT_STEP_CONNECTED_MARKERS` (default `connected`),
   exact after normalising. Otherwise not in, not listed.
2. `row_gate` fails → out (stopped or snoozed; `run()` already counts those).
3. `_date(row, "li_connected_date")` is None → report `no_date`, log at INFO, out.
4. state `closed` → report `closed`, out (section 6).
5. Meeting Status completed (`gtm_sheet.is_meeting_completed`) → report `completed`, out.
6. `NEXT_STEP_PAUSE_FOR_BOOKED_MEETING` and Meeting Date ≥ today → report `paused`, out.
7. Step code from Q (5.4). Unknown → report `unknown_step` with the raw value, log, out.
8. Compute (ask, due, missing) from 5.3. `today < due` → report `waiting`, out.
9. Otherwise the person is DUE.

### 5.3 The timing table
`+Nd` is calendar days; "due" means `today >= that date`. No weekend shift: the rule only
runs on its weekdays, so a Saturday due date is served on Monday.

| Variable | Default |
|---|---|
| `NEXT_STEP_FIRST_DAYS` | 2 |
| `NEXT_STEP_AFTER_PREVIOUS_DAYS` | 2 |
| `NEXT_STEP_AFTER_EMAIL_DAYS` | 7 |
| `NEXT_STEP_CALL_AFTER_DM_DAYS` | 7 |
| `NEXT_STEP_CALL_EVERY_DAYS` | 3 |
| `NEXT_STEP_CALL_UNTIL_DAYS` | 21 |

`sent(N)` = `parse_flag(email_N_sent) is True`. `date(N)` = `sheet_date(email_N_date)`.
`C` = LI Connected Date. `DM` = `sheet_date(li_dm_date)`.

| # | Step (Q) | Also | Due | Ask code |
|---|---|---|---|---|
| 1 | blank | | C + FIRST | `ask_next` |
| 2 | Research the PoC | | C + FIRST | `researched` |
| 3 | Send email N | not sent(N) | prev + AFTER_PREVIOUS; prev = C for N=1, date(N-1) otherwise; prev blank → today | `email_out` |
| 4 | Send email N | sent(N), date(N) | date(N) + AFTER_EMAIL | `advance` (to "Send email N+1"; after 3, "Reach by LI DM") |
| 5 | Send email N | sent(N), no date(N) | today | `log_date` |
| 6 | Reach by LI DM or Call the PoC | LI DM Sent is a replied marker (default f) | today | `dm_replied` |
| 7 | Reach by LI DM | no DM | date(3) + AFTER_PREVIOUS; date(3) blank → today | `dm_out` |
| 8 | Call the PoC | no DM | today | `call_no_dm_date` |
| 9 | Reach by LI DM or Call the PoC | DM, today < DM + CALL_AFTER | not due (`waiting`) | |
| 10 | same | DM + CALL_AFTER ≤ today ≤ DM + CALL_UNTIL | no call reminder sent yet for this signature, or today ≥ last call date + CALL_EVERY | `call` (**a call reminder**) |
| 11 | same | today > DM + CALL_UNTIL | today, once | `unresponsive` |

Row 6 is tested before 7 to 11. Rows 3 and 7, `missing` (cells the line also asks for,
"go by Q and also ask for the missing cell"): for email N ≥ 2, "(N-1)th Email Sent" when
not sent(N-1), and "(N-1)th Email Date" when date(N-1) is blank; for row 7, "3rd Email
Sent" and "3rd Email Date" the same way. A date cell holding text that does not parse
counts as blank for the due date and is named in `missing`.
Row 3 applies whenever sent(N) is not True, even if date(N) holds a date.
Row 10's line adds "set Next Steps to Call the PoC" only while Q is "Reach by LI DM".
With the defaults and a post every due weekday, a DM on a Monday gives call reminders on
days 7, 10, 14, 17, 21 and the Unresponsive reminder on day 22.

### 5.4 Reading Q
`code = step_code(raw)`: normalise with `gtm_sheet.normalise_header`, drop the word
"the", then: "" → `blank`; "research poc" → `research`; `send e ?mail ([123])` →
`email1..3`; "reach by li dm" or "reach by linkedin dm" → `dm`; "call poc" → `call`;
anything else → `unknown`. So "Send Email 1" = "send email 1", "Call PoC" = "Call the
PoC". The labels printed in a line are the dropdown's own six, held as constants.

### 5.5 Signature (what "the step changed" means)
`stage|cells`, built from the row:
- `blank` → `start|<C iso>` · `research` → `research|<C iso>`
- `emailN` → `emailN|<yes or blank>|<date(N) iso or blank>`
- `dm` and `call` → `dm|<normalised LI DM Sent>|<DM iso or blank>`
`dm` and `call` share one stage on purpose: Rule 13 itself asks people to change Q from
"Reach by LI DM" to "Call the PoC", and doing so must not restart the call clock.

### 5.6 Selection
`due` people, each with their state entry (ignored when its signature differs from the
row's: that is "state cleared", with no write):
```
calls  = [p for p in due if p.ask == "call"]
others = [p for p in due if p.ask != "call"]
key    = (state.last_date or "", sheet_row)      # never mentioned sorts first
picked = (sorted(calls, key) + sorted(others, key))[: rule.max_items_per_post]
```
Nobody due → no items → no post. The evaluator returns one item per picked person, each
with `pick_order` 0..n-1. Everyone else due is reported as `queued`.

### 5.7 The item
`_item(rule=…, trigger=R_NEXT_STEPS, today, due=<due date>, company, poc, designation,
sheet_row, row_key=contact_key=activation.row_key(row), why=<internal reason naming the
table row and the variables>, text=<the line>, extra={…})` with extra:
`dayof_time=config.NEXT_STEP_TIME`, `step` (code), `step_label`, `ask`, `signature`,
`missing` (list), `pick_order`, `opener_index = today.toordinal() % 3`,
`poc_priority` (raw AF), `email_n` (1..3 or 0).

### 5.8 The report (for the preview and the log)
The evaluator writes `ctx["reports"][rule.id] = {"connected": n, "due": n, "picked": [...],
"queued": [...], "waiting": n, "no_date": [...], "unknown_step": [...], "paused": [...],
"completed": n, "closed": [...], "state": "ok" | "unreadable"}`; entries are
`{sheet_row, poc, company, detail}`. `run()` creates `ctx["reports"] = {}` and returns it
as `result["reports"]`. `preview_text` prints, after the rule sections, "**R13 · who is
not in today's post**" with each non-empty list (names and sheet rows) and the counts.
One INFO log line per evaluation with the counts, and one per `no_date` / `unknown_step`
row.

---

## 6. State

### 6.1 Tables (db.py SCHEMA, beside `meeting_followups`)
```
CREATE TABLE IF NOT EXISTS next_step_followups (
    row_key        TEXT PRIMARY KEY,           -- activation.row_key
    signature      TEXT NOT NULL DEFAULT '',
    last_date      TEXT NOT NULL DEFAULT '',   -- ISO day Rule 13 last named them, this signature
    mentions       INTEGER NOT NULL DEFAULT 0, -- this signature
    calls          INTEGER NOT NULL DEFAULT 0, -- call reminders sent, this signature
    last_call_date TEXT NOT NULL DEFAULT '',
    last_ask       TEXT NOT NULL DEFAULT '',
    closed         INTEGER NOT NULL DEFAULT 0, -- the Unresponsive reminder went out
    closed_date    TEXT NOT NULL DEFAULT '',
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS next_step_posts (   -- what each post said, for NFT2-1063
    message_id TEXT PRIMARY KEY,
    on_date    TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    people     TEXT NOT NULL DEFAULT '[]'      -- JSON list, post order
);
```
`people` entries: `{row_key, sheet_row, poc, company, step, step_label, ask, email_n,
signature, line}`.

### 6.2 Methods
- `db.next_step_state() -> dict | None`: `{row_key: {signature, last_date, mentions,
  calls, last_call_date, last_ask, closed, closed_date}}`. **Fails CLOSED: returns None on
  any error** (logged at ERROR). With None the evaluator emits nothing and reports
  `state: unreadable`. An empty dict would restart the rotation and chase people already
  closed.
- `db.record_next_step_mention(row_key, *, signature, on_date, ask)`: if the stored
  signature differs, reset `mentions`, `calls`, `last_call_date` to zero/blank first (the
  step changed), then set signature, `last_date=on_date`, `mentions+1`, `last_ask`; when
  `ask == "call"`: `calls+1`, `last_call_date=on_date`; when `ask == "unresponsive"`:
  `closed=1`, `closed_date=on_date`. `closed` is never reset by a signature change.
- `db.record_next_step_post(message_id, *, on_date, channel_id, people)`,
  `db.next_step_post(message_id) -> dict | None` (read side, used by tests now and by
  NFT2-1063 later).
- Both tables are operational state: not in `KEPT_ON_RESET` (db.py:978).
  `clear_test_day` (db.py:3651) is unchanged.

### 6.3 Who reads and writes
- Read: `bot._run_next_actions` (bot.py:9352-9366 block) reads `next_step_state()` and
  passes `next_step_state=` to `nextaction.run` (new keyword, into `ctx`). Nothing is
  reset there: a changed signature is handled by ignoring the entry (5.6). So the preview
  and every question write nothing for Rule 13.
- Write: `bot._after_send` (bot.py:8910) gains a branch for `R_NEXT_STEPS`, calling a new
  `_record_next_steps(message, sent=sent, marker=marker)`: for each action in
  `drip.shown_contacts(message)`, `record_next_step_mention`; then one
  `record_next_step_post`. It runs only after `guardrails.send` returned a message
  (bot.py:8370-8376 returns before it otherwise).

### 6.4 Staying off live state
| Path | What happens |
|---|---|
| `cadence preview`, `--dry-run-drip`, any question | pure: reads state, writes nothing |
| simulation | `self.db` is the sandbox copy; records there; discarded |
| live | records in `DB_PATH` |
| SALES_TEST_MODE=true | identical to live, test channel, records in `DB_PATH`. Same as every other ledger; the existing instruction (bot.py:7866-7869) is to point `DB_PATH` at a `*_test.db` first. Stated in README and the deploy notes. |
| test day ("make it Monday") | records only when `DB_PATH` ends in `_test.db` (the same test `_clear_stale_test_day` uses, bot.py:11388-11389). On any other database `_record_next_steps` writes nothing and logs "[rules] R13: test day on a live database; rotation not recorded". The post itself is identical. |

The test-day row is stricter than R9 (L5). It is Q5.

---

## 7. Timing

### 7.1 Where 15:00 comes from
`config.NEXT_STEP_TIME` (`.env`, default "15:00", validated HH:MM at startup with a
WARNING and fallback to 15:00). Each item carries it as `dayof_time`; `drip.group` lifts
it to the group (drip.py:166-168); `drip.pinned_time` returns it (525-543). A pinned post
takes no window slot and the window cannot roll it (drip.py:799-830). Add
`config.NEXT_STEP_TIME` to the tuple in `drip.earliest_send_ist` (557) so a time set
before the window opens is planned. Rewrite `pinned_time`'s docstring to name three
fixed-time posts.

### 7.2 drip.py
- `NEVER_HELD` += `R_NEXT_STEPS` (its own clock; the re-ask clock would silence it every
  other day and reword it as "one last nudge").
- `RULE_ONLY_TYPES` += `R_NEXT_STEPS`: group key `next_step_followups|*`, one post a day.
- NOT in `NEVER_COUNTED`: `counts()` reads the item's `counts_toward_cap`, which is the
  rule's (false). Default b stays editable in the file.
- NOT in `PLANNED_FIRST`.
- Volume-contract docstring (drip.py:25-37): add the line "next-step follow-ups go at
  NEXT_STEP_TIME on weekdays and do not count".

### 7.3 Two posts in one minute, today, and the two guard changes
Today: the sender posts ONE message per tick, `due[0]` in plan order (send time, then
`PLANNED_FIRST`, then rank; drip.py:837-839, bot.py:7915). After any send the catch-up
guard holds everything for `drip.min_gap_minutes()` from the last ACTUAL send
(bot.py:7843-7848): 30 minutes at the `.env.example` defaults, 1 minute on this laptop.
So two pinned posts, or a pinned and a spaced post, due in the same minute go out one
tick apart at best and `min_gap_minutes()` apart at worst. At the defaults R1 goes at
14:00, the first spaced post at about 14:30, and a 15:00 post is on the edge of the
guard: a spaced post that went at 14:31 holds R13 until the tick after 15:01.

Build, narrowly (NFT2-1069 replaces the whole guard, section 15):
- `drip.ON_TIME_TYPES = frozenset({nextaction.R_NEXT_STEPS})`, with a comment saying why.
- `_maybe_send_drip`: do not return at 7847. Remember `gap_held`. After `due` is built
  (7862), when `gap_held` keep only messages whose `type` is in `ON_TIME_TYPES`; return if
  none. Order of the other steps unchanged (kill switch, plan, channel checks, sweep).
- `_last_drip_sent_at` (7959): skip rows whose `action_type` is in `ON_TIME_TYPES`, so the
  15:00 post never delays a spaced post either. Gains nothing else.
R1 and R8 keep today's behaviour exactly.

### 7.4 Weekdays, test day
`weekdays: [mon, tue, wed, thu, fri]`. `rules.for_day` excludes R13 on Saturday and
Sunday; the Sunday branch of `drip.plan` only passes `SUNDAY_RULE_IDS`. On a test day and
in a simulation the post goes at the 14:00 stop with `send_at_hhmm` "15:00"
(bot.py:11292-11300), like every afternoon post.

---

## 8. The post

### 8.1 bot_rules.yaml
Insert R13 between R4 and R5 (F1), with a comment saying the position is deliberate: "one
contact is named once a day and the earlier rule keeps them; R13 names 5 people and R6
selects every connected contact, so R13 goes first". Ids are not renumbered.
```
  - id: R13
    name: Next steps for connected contacts
    plain: next steps for the people we're connected with
    sheet_wording: >-   (section 12)
    weekdays: [mon, tue, wed, thu, fri]
    trigger: next_step_followups
    max_items_per_post: 5
    destination: channel
    counts_toward_cap: false
    enabled: true
```
The comment block above it carries the timing table with the variable names, the rotation
in two lines, defaults a to g, and "the post time is NEXT_STEP_TIME until NFT2-1069 adds
fixed_time".
R7: `enabled: false`, comment: "REPLACED BY R13 on 7 Oct 2026 (Vaishnavi: 'rule 13
supercedes this'). Every row with an LI DM Date is Connected, and R13's call reminders
start 7 days after the DM. The evaluator is kept." Its `sheet_wording` becomes the
"replaced" sentence (section 12).
R6: comment and nothing else in the file unless Q3 says yes.
R9: `sheet_wording` and the comment say Notes/Remarks (section 12).
`global_rules.daily_cap`: "Max 5 posts a day; meeting prep, meeting follow-ups, next-step
follow-ups, reminders, urgent news and answers to questions don't count."
Header comment: "R1..R13", and the field notes that mention R8/R9 as the only uncounted
rules.

### 8.2 wording.py (all Rule 13 text lives here)
New section "the next-step follow-ups". Add to the module docstring that this is the one
drip family held here, because its lines are fixed and its reply lines (NFT2-1063) will
quote the same cells. Functions, all pure, all added to `all_lines()`:
`NEXT_STEP_OPENERS` (three), `next_step_line(ask, *, who, step_label="", n=0, when="",
missing=(), set_call=False)`. `who` is "Name (Company)", or the company alone when the
row has no name. `when` is a sheet date as "5 Oct". Proposed wording (Q2):
```
openers       A few next steps on people we're connected with:
              Next steps for some of our LinkedIn connections:
              Where a few of our connections stand:
ask_next      {who}: Next Steps is blank. What's the next step? Usually it's Research the PoC.
researched    {who}: Next Steps says Research the PoC. Have they been researched? If so, set Next Steps to Send email 1.
email_out     {who}: Next Steps says Send email {n}. Has it gone out? If so, mark {nth} Email Sent and the date.
advance       {who}: email {n} went out on {when}. Time to set Next Steps to {next label}.
log_date      {who}: {nth} Email Sent is marked, but there's no date. Can you log when email {n} went out?
dm_out        {who}: Next Steps says Reach by LI DM. Has the DM gone out? If so, log LI DM Sent and the date.
call          {who}: the LI DM went out on {when} and there's no meeting yet. Time to call them.
   set_call   …Time to call them, and set Next Steps to Call the PoC.
call_no_dm_date  {who}: Next Steps says Call the PoC. Have they been called? Please log the LI DM Date too.
dm_replied    {who}: they replied to the LI DM. Is a meeting being set up?
unresponsive  {who}: still no meeting after the calls. Please set Prospect Status to Unresponsive and I'll stop asking about them.
missing       appended: " {A} and {B} aren't filled in either." (one cell: "isn't")
```
Rules for every line: `wording.register_problems(line) == []`; no rule number; no
schedule words (no "every", "daily", "3 PM", "days"); no setting name; none of
`llm.BANNED_PHRASES`; every name, company and date comes from the row.

### 8.3 drip.py rendering
- `VERBATIM_TYPES` += `R_NEXT_STEPS`: posted exactly as rendered, never composed by the
  model. So the same queue gives the same post live, in test mode, on a test day and in a
  simulation, and no model can move a step from one person to another.
- `points_of`: branch for `R_NEXT_STEPS`: members sorted by `pick_order`; header =
  `wording.NEXT_STEP_OPENERS[opener_index]` (from the first member, so it is a function
  of the day and never random); lines = `"• " + action["text"]`; no extra, no close.
- `HEADINGS["R13"] = "Next steps"`, `_HEADING_BY_TYPE[R_NEXT_STEPS] = "R13"` (Q2).
- `nothing_to_say`: True for `R_NEXT_STEPS` with no actions (cannot happen; belt).

### 8.4 Addressing
The other Outreach PoCs rules leave `owner` empty and are addressed by the tags line
(`drip.with_tags` → `SALES_ALWAYS_TAG_IDS`) and, when composed, `_drip_mention`. R13
does the same. Change `drip.group` (170-173): the owner of a `RULE_ONLY_TYPES` group is
`config.DELIVERABLE_DEFAULT_OWNER` only for `R_DELIVERABLES`; for any other rule-only
type it is `owner_label(best)` / `owner_key(best)`, which is empty for R13. No user id
appears in code. Mentions come from `guardrails.mention_for` as everywhere else.

### 8.5 Voice rules, the guard, and what the post does NOT pass through
- The reply guard (`replyguard.clean`) is called in one place, on ANSWERS
  (bot.py:2544). No proactive post passes through it, so it cannot strip Rule 13's opener
  or its name lines. (If it ever did: the opener ends in a colon and newline and the
  lines are a list; `_POINT_RE` lists are protected.)
- `llm.proactive_verdict` runs only on model-composed posts; a verbatim post skips the
  model entirely (bot.py:8250-8251).
- What does apply: `guardrails.send` (its `_for_people` strips rule ids;
  `rules.render_for_user` returns text unchanged when nothing matched), `with_sources`,
  `with_tags`, `with_heading`, `_tag_test`.
- No web search, no email lookup (`_request_email_lookups` is R5/R6 only, bot.py:8584),
  no suppress-or-convert (`evidence.stage_of` is falsy for this type), no proposal.
  A Rule 13 send makes zero model calls and zero searches.

### 8.6 Other rules
- R6, decided: nextaction.py:954-955 becomes
  `f"{_describe(row)} — connected {elapsed} day(s) ago. " + (email on file | No email on file)`.
  Docstring 928-934: R6 is the email check; the DM belongs to R13's sequence.
  sales_policy.md:232-236 exemplar: drop "and there's no DM logged". Q3 covers the rest.
- R7: not evaluated (`rules.for_day` drops a disabled rule; `nextaction.run` reports
  "disabled in bot_rules.yaml", nextaction.py:1434).
- R9: no logic change. It reads `next_steps`, which is Z after section 4.

### 8.7 Other call sites
bot.py:4808 (tool description listing triggers): add `next_step_followups`. bot.py:5299
and research.py:267 (row fields handed to the model): add `outreach_step`, the email
roles and `poc_priority` so answers can quote them. README rule table and counts.

---

## 9. Files, in build order

1. `gtm_sheet.py`: 4.2, 4.3, 4.5 text, 4.6 guard in `write_cells_on`. Self-test: both
   header rows; the veto.
2. `config.py`: band defaults, the warning, comments; the NEW variables (section 10) with
   WHY comments beside `LI_NO_DM_DAYS` (config.py:2059); startup validation of
   `NEXT_STEP_TIME`; a WARNING when `NEXT_STEP_CALL_UNTIL_DAYS < NEXT_STEP_CALL_AFTER_DM_DAYS`.
3. `.env.example`: same defaults, the 32-column table, the new variables uncommented.
4. `sheetwrite.py`, `db.py` (text; tables and methods of section 6).
5. `rules.py`: trigger, plain wording, self-test (13 rules; file order R1 R2 R3 R4 R13 R5
   … R12; Monday `[R1,R4,R13,R8,R9,R10,R11]`; Thursday `[R1,R13,R5,R8,R9,R11,R12]`;
   Saturday `[R8,R9]`; R7 disabled; R13 outside the cap).
6. `wording.py`: 8.2.
7. `nextaction.py`: constants, `step_code`, `step_signature`, the evaluator, `run()`
   keyword `next_step_state` and `ctx["reports"]`, `preview_text` block, R6's line,
   module docstring ("THE THIRTEEN"), self-test block for the table.
8. `drip.py`: 7.1, 7.2, 8.3, 8.4.
9. `bot.py`: `_run_next_actions` (read state), `_after_send` + `_record_next_steps`,
   `_maybe_send_drip` + `_last_drip_sent_at` (7.3), the reply guard below, 8.7, 9020.
10. `bot_rules.yaml`: 8.1.
11. Docs: section 11. `docs/rule13-for-the-sheet.md`: section 12.

### 9.3 The reply guard built now (F2)
In `_maybe_vote_on_proposal`, where `guessed and ref` (bot.py:6351): look the replied-to
message up with `db.find_drip_by_message_id(str(ref))`; when its `action_type` is the
Rule 13 trigger, log "[approvals] msg=… replies to a next-steps post — not a vote" and
return False. A proposal attached to that very message is still found first (6337-6340),
so nothing else changes. Not widened to other posts: that is NFT2-1063's.

---

## 10. Environment

**NEW** (all in `.env.example`, uncommented)
```
NEXT_STEP_TIME=15:00
NEXT_STEP_CONNECTED_MARKERS=connected
NEXT_STEP_FIRST_DAYS=2
NEXT_STEP_AFTER_PREVIOUS_DAYS=2
NEXT_STEP_AFTER_EMAIL_DAYS=7
NEXT_STEP_CALL_AFTER_DM_DAYS=7
NEXT_STEP_CALL_EVERY_DAYS=3
NEXT_STEP_CALL_UNTIL_DAYS=21
NEXT_STEP_DM_REPLIED_MARKERS=replied,responded
NEXT_STEP_PAUSE_FOR_BOOKED_MEETING=true
```
**CHANGED default**: `RESTRICTED_COLUMN_RANGES` `A:I,S:X` → `A:I,Q:W,Z:AE`;
`NEW_ROW_WRITABLE_RANGES` `A:R` → `A:P,X:Y`.
**RETIRED**: none. `DM_NO_MEETING_DAYS` and `DM_NO_MEETING_MAX_CONTACTS` stay (R7's
evaluator is kept); their comments say R7 is switched off.

**Laptop `.env`** (agents do not edit it):
```
NEW_ROW_WRITABLE_RANGES=A:P,X:Y        <- is A:R today; must change
RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE  <- already set
GTM_COLUMN_MAP=                        <- already blank; leave blank
```
The ten NEW lines are optional; the defaults apply without them.
Recommended, a human decision (Q7): `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=15` (is 150).
Then `python tools/redact_env.py`.

**Server `/opt/sales-bot/.env`** (not visible; check each):
```
RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE
NEW_ROW_WRITABLE_RANGES=A:P,X:Y
GTM_COLUMN_MAP=
```
and confirm `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` is 15 or unset.

---

## 11. Docs (builder)
- README: "The twelve rules" → thirteen (83, 1904-1989 table: R13 row, R7 "off, replaced
  by R13"), the columns section (863-889), bands (57, 1207, 1411, 1545-1547, 1620,
  2576-2580, 2826), the state table (2234: add `next_step_followups`), the cap table
  (2919), a new section "Rule 13: next steps for connected contacts" (the table, the
  rotation, state, what a reply does today, the test-day note), verify list (4902).
- DEPLOY.md: "RULE 13: set the three lines in /opt/sales-bot/.env, restart, read the
  `[gtm.window]` line (J:P, X:Y), the role report (outreach_step and email roles mapped)
  and the `[rules]` block (13 rules, R7 DISABLED). The two tables are created on boot."
- sales_strategy.md section 7: weekly schedule (Monday loses "DM sent with no meeting";
  add a row "Monday to Friday, 3 PM · next steps for connected contacts"), daily cap
  sentence, "The 13 rules", rule 6, rule 7 ("Replaced by rule 13 on 7 Oct 2026"), rule 9
  ("Filling in Notes/Remarks ends it"), new rule 13. The same text goes into the Google
  doc by hand if `STRATEGY_DOC_ID` is ever set (empty today).
- sales_policy.md: R6 exemplar (232-236), R7 exemplar marked replaced (238), R9 (250:
  "Notes/Remarks"), a Rule 13 exemplar (the register sample), and one line under the
  write rules: Saley reminds about Q to W and never fills them.
- docs/SOURCES.md:25: bands, and "Next Steps (Q) and the email columns are read, never
  written".
- CLAUDE.md: the lead edits it (text in the planner's reply).

## 12. docs/rule13-for-the-sheet.md (builder writes; plain words for Vaishnavi)
1. Rule 13's Bot Rules cell = R13 `sheet_wording`: who (Connected with an LI Connected
   Date), when (weekdays, 3 PM), how many (5), the order (top of the sheet down, then
   from the top again; call reminders first), what each step is asked, the call reminders
   and the Unresponsive reminder, "Saley only reminds; it never fills these cells",
   "doesn't count toward the 5 posts a day".
2. Rule 7: "Replaced by rule 13 from 7 Oct 2026."
3. Rule 6: the changed line, and Q3's wording if approved.
4. Rule 9: "…whose Notes/Remarks is blank… Filling in Notes/Remarks ends it."
5. Weekly Schedule line, and the Global Rules "Daily cap" row.
6. Defaults a to g as seven yes/no questions.

---

## 13. Tests

Fixtures use made-up names (Priya Rao, Acme Labs, …), `.env.example` defaults pinned with
monkeypatch (never the laptop's `.env`), temp DBs, `offline_guard`. Fixed dates: Mon
2026-10-12 to Fri 2026-10-23. Written from this plan, not from the builder's code.

### 13.1 New `tests/test_rule13.py`
| # | Case | Expect |
|---|---|---|
| H1 | today's header row through `_parse_values` | every role in 4.1 on its column; `next_steps` is Z; `outreach_step` is Q |
| H2 | pre-7 Oct row | `next_steps` is S; `outreach_step`, email roles, `poc_priority` unmapped; all 24 old roles as before |
| H3 | veto | a header "Next Steps" can never become `next_steps`, even with `GTM_COLUMN_MAP={"outreach_pocs":{"next_steps":"Next Steps"}}` (warning logged); "Next Steps / Notes (new)" never becomes `outreach_step` |
| H4 | R9 on today's row | follows up when Z is blank (Q filled); silent when Z has notes (Q blank) |
| B1 | defaults | `writable_window_label()` == "J:P, X:Y"; `[gtm.window]` log names J..P, X, Y |
| B2 | existing row | `write_cells` refuses roles on Q, R, W, Z, AE, each naming its band; a J or X role is planned |
| B3 | new row | `append_row` (fake worksheet, as tests/test_signed_row_append.py) fills only A:P and X:Y; `outreach_step`, `email_1_sent`, `next_steps`, `closure_prob` refused; Sr No, Company/Uni, Name (with note), LI Url written |
| B4 | `write_cells_on(pocs_tab, …)` | refused |
| B5 | tiers | `allowed_for` False for the eight new roles, both triggers |
| B6 | startup warning | new-row `A:R` against `A:I,Q:W,Z:AE` warns and names Q:R; `A:P,X:Y` does not |
| T1-T11 | one per table row | due date, ask code, line; including a blank previous date (due today, `missing` named), DD.MM.YYYY dates, "Send Email 1" / "Call PoC", an unknown Q (skipped, in the report), Q "Send email 2" with 1st Email Sent blank |
| C1 | call clock, DM on Mon 2026-09-21, a send on each due weekday | call reminders on days 7, 10, 14, 17, 21; `unresponsive` on day 22; nothing on day 23 or ever after, even after Q is changed |
| C2 | Q changed dm → call mid-clock | no extra reminder; count continues |
| C3 | Prospect Status "Unresponsive" | not in, at any step |
| C4 | default f | "Replied": `dm_replied`, never `call`, never `unresponsive`; with the marker list emptied it follows the call clock |
| C5 | default g | meeting tomorrow: paused, listed; meeting last week, not Completed: carries on; Completed: out; with the switch false a future meeting does not pause |
| R1 | 33 Connected rows, 29 dated (6 as DD.MM.YYYY), steps 18 / 11 / 4, all due; 10 weekdays, recording after each | 5 a day; days 1 to 5 are rows 1 to 25 in sheet order; day 6 is rows 26 to 29 then row 1; day 10 ends at row 21; nobody twice before everyone once; the 4 undated in `no_date` every day and logged |
| R2 | one person's call reminder due on day 3 | first line of that day's post |
| R3 | a step change on day 2 for a row named on day 1 | named again on day 2, ahead of never-named rows below it |
| R4 | nobody due | no item, no post, no state row |
| R5 | state unreadable (`next_step_state` None) | no items; report says so |
| D1 | Tuesday, shipped rules, a contact both R6 and R13 select | R13 keeps them; R6's copy is in `deduped` |
| O1 | shipped rules | R7 produces nothing on a Monday; R6's text has no "no DM logged"; 13 rules load |
| P1 | plan on a weekday | one R13 message, `pinned`, 15:00, `counts_toward_cap` False, stage nudge two days running (never held), cap of 0 does not roll it, the spaced posts' times are the same with and without it |
| P2 | Saturday, Sunday | no R13 message |
| P3 | sender | a spaced post sent 5 minutes ago does not hold the 15:00 post; the R13 row does not delay the next spaced post |
| S1 | send through `_send_drip_message` with fakes | state rows for the 5, one `next_step_posts` row with the 5 people; a refused send records nothing |
| S2 | preview / `_run_next_actions` twice | both tables byte-identical before and after |
| S3 | test day with `DB_PATH` not ending `_test.db` | post sent, tables unchanged, the log line; with `_test.db`, recorded |
| S4 | simulation | real DB unchanged |
| S5 | live vs SALES_TEST_MODE, same queue | bodies equal apart from the leading tag |
| V1 | every line of 8.2 | `register_problems == []`, no banned phrase, no rule id, no schedule word |
| G1 | approver replies "yes" to an R13 post while an `email_write` proposal is open | no vote recorded, proposal still open, zero sheet writes |
| G2 | "done" replied to an R13 post (fake extractor returning none) | zero sheet writes, zero Rule 13 state change |

### 13.2 New `verify_rule13.py`
Offline, `offline_guard.install_script()`, fake Discord, temp DB, `check(name, got,
want)`, ends ALL PASSED or N FAILED. Prints, so a person can read them: the role map for
both header rows; the window line; the ten-weekday rotation as a table; one post as
Discord would show it, four ways (live, test mode, test day, simulation); the call clock;
the preview block; the `.env` lines.
One extra mode, **`--real-sheet --date 2026-10-08`**, for the final dry run only: exactly
one `gtm_sheet.SHEETS.read()`, read-only; Rule 13 evaluated with an EMPTY state (nothing
has been sent yet) through `nextaction.run(day_rules=[R13])`; no database at `DB_PATH`
opened, no Discord, no model; prints the header row, the counts (expect 33 Connected, 29
dated, 4 undated; 18 / 11 / 4), the 5 picked with each line, and the report lists. It
must refuse to run a second read. The tester runs it once, last.

### 13.3 `verify_replay_oct6.py`: extend, never fork
Add a "RULE 13" section after the profile section, with its own entry in the header
docstring: a stubbed sheet in the 7 Oct layout with made-up rows; a Rule 13 post sent
through the real `_send_drip_message` with SALES_TEST_MODE false then true (bodies equal
apart from the tag); then "done" and "yes" replied to it through the real `on_message`
(G1, G2: zero writes, the open proposal untouched). Replies proper stay a printed SKIP
"owned by NFT2-1063".

### 13.4 Existing tests and scripts that pin the old layout or old rules
Rule: replace the expectation with the new rule, keep every check that is not about the
thing that changed, never delete a check without its replacement beside it.

| File | What pins | Legitimate update |
|---|---|---|
| tests/conftest.py | `POCS_HEADERS` (19-25) and the 24-cell rows | `POCS_HEADERS` becomes the 7 Oct row (32); rows widened; keep the old row as `POCS_HEADERS_PRE_7OCT` for H2 |
| tests/canned_sheet.py | its own `POCS_HEADERS` | same row as conftest; rows are built by index 0 to 6, unchanged |
| tests/test_signed_row_append.py | `"A:R"` at 109 | `"A:P,X:Y"`; 248 (closure refused) and 294 (`A:H`) stay |
| tests/test_regressions.py | 385-400 new-row commercial block; 666 `len(words) == 12` | still refused on the new layout; 13 |
| `python -m rules`, `python -m nextaction`, `python -m sheetwrite`, `python -m wording` | counts, order, R6 text, the old schema | per section 9 |
| `python -m gtm_sheet` | **NOT an offline self-test: it is a LIVE ACCESS CHECK that reads the real sheet.** Never run it in this work (it was listed here in error and one run read the real sheet). | header mapping and the veto are checked in `python -m sheetwrite` and tests/test_rule13.py H1 to H3 |
| `python drip.py` | red at baseline (laptop gap values); 2376-2378 pins an R6 template | add R13 checks; update the R6 line only if Q3 is yes; the baseline failures stay |
| verify_approvals.py | bands pinned `A:I,S:X` (42-44), old header row (71) | 7 Oct row, bands `A:I,Q:W,Z:AE`; every check kept |
| verify_s3.py (green) | (ii) R7 on a Monday through the live sweep (616-636); own old header row (178) | keep its header row (it becomes the pre-7 Oct check); for (ii) load a temp copy of bot_rules.yaml with R7 enabled, with a comment that the evaluator is kept and the rule is off as shipped; add one check that with the shipped file R7 does not post; `the_wording` 952 still finds R7's wording |
| verify_s1.py (red at baseline: the tag) | Monday fixture counts R7 as one of six counted groups (289-294, 586-605) | same temp-rules approach, or re-derive the counts with five counted groups; say which in the report. Check whether any fixture row is "Connected" with a date; if so R13 is an eighth post and the counts say so |
| verify_parity.py --fixture-only, verify_research_timing.py, verify_news_feed.py, verify_search_backend.py --stub | canned sheet layout | re-run; fix only what the layout moved |
| verify_layouts.py | R6 heading and template strings (195, 226, 244) | NOT run. Edit by reading only if Q3 is yes; say "unverified" in the report |
| verify_monday, verify_heavy_monday, verify_tone, verify_voice, verify_voice_profile, verify_testday, verify_testday_talk | hand-built R6/R7 items passed straight to `drip.plan` | untouched |

### 13.5 The red baseline
Rule 13 touches: `python drip.py`, verify_s1, verify_parity --fixture-only,
verify_search_backend --stub (the last two only through the canned sheet). Each must be
no redder than before, with the same failing lines; the report shows before and after.
**Everything else on the baseline list stays red**: pytest
`TestPostingWindow::test_a_heavy_day_shrinks…`, verify_monday, verify_heavy_monday,
verify_clock, verify_testday, verify_testday_talk, verify_test_output, verify_reminders,
verify_s2, verify_websearch --offline. "Every verify_*.py and pytest is green" is not
reachable inside this work unless the human widens scope (Q8).
Never run: verify_poc_lookup, verify_layouts, verify_simulation, verify_search_backend
without --stub, verify_parity without --fixture-only, anything that calls the model or
the network.

### 13.6 docs/test-reports/RULE13.md must contain
1. Commands run and their last lines (py_compile on every changed file, pytest, each
   self-test, verify_rule13, verify_replay_oct6, every script in 13.4 in its offline mode).
2. The offline-guard line from each run, and the count of real-sheet reads (0, then 1).
3. H, B, T, C, R, D, O, P, S, V, G: pass or fail; the printed rotation table and one post.
4. The baseline table: before and after counts for every red script.
5. Every existing check changed: file, line, old, new, the section here that justifies it.
6. Values pinned by the tests, and the laptop's real values where they differ (section 1).
7. The final dry run: the 5 people and each one's line for Thu 8 Oct, the counts against
   the ticket's, the four undated rows, any unknown Q values. "Posted nothing, wrote
   nothing", with the evidence.
8. What was not run and why. Anything this plan got wrong.

---

## 14. Replies to a Rule 13 post (BUILD NOW; NFT2-1063 has landed)

Built against what NFT2-1063 put in the tree: `bot._reply_context` (bot.py:1182), the
acknowledgement path `_maybe_acknowledge` (1367), the stub `_maybe_next_step_reply`
(7270, called at 6806), `replies.py`, `_remember_said` (1171). Edits to bot.py are small
and targeted; tests/test_replies.py, verify_replies.py and the replay stay green.

### 14.1 What it does
A reply to a Rule 13 post that says the thing is done gets one fixed line asking for the
sheet update that step needs. No sheet write, no proposal, no vote, no model call, no
sheet read, no change to Rule 13 state. Anyone on the team may say it (default d).

### 14.2 Where the hook sits
`_handle_query`, straight after `self._reply_ctx[message.id] = ctx` (bot.py:~1527) and
BEFORE `_maybe_acknowledge`:
```
if self._is_next_step_reply(ctx) and await self._maybe_next_step_reply(message, text, ctx):
    return True
```
`_is_next_step_reply(ctx)`: `ctx["direct_is_bot"]` and either
`(ctx.get("drip") or {}).get("action_type") == nextaction.R_NEXT_STEPS` or
`(ctx.get("said") or {}).get("kind") == "next_step_which"`.
So under a Rule 13 post a bare "done", "yes" or "sure" reaches the hook before the
acknowledgement path and before `_maybe_vote_on_proposal`. An approver's "yes" there is
never a vote and applies nothing. The existing call at 6806 stays as it is (for the same
message it returns the same False); only its comment changes.
The hook sets `self._mark_route(message, "next_step_reply")` when it handles a message.

### 14.3 The hook, in order
1. `post = db.next_step_post(root)`, where `root` is `ctx["root_id"]` for a reply to the
   post and `said["root_id"]` for a reply to the "which one?" line. No record (a test
   day on a live database records none) → return False: handled as before.
2. A reply to the "which one?" line: candidates are `said["which"]` (row keys, in the
   order shown). Pick by `replies.pick_numbered(text, n)`, else by name (14.4), else
   `replies.next_step_all(text)` ("all", "both", "everyone") = all of them. Nothing
   picked → return False. Picked → step 6.
3. `kind = replies.next_step_done(text)` ("" | "done" | "researched" | "sent").
4. `kind == ""`:
   - `replies.is_ack(text, names)`, or `replies.is_bare_vote(text, names)` is "yes" or
     "no" ("sure", "ok", "thanks", a thumbs-up, "no", "not yet") → `self._react(...)`,
     return True. One reaction, nothing else. They will be asked again by the rotation.
   - anything else (a question, a sentence) → return False. A question goes to the
     engine with the post as context (`replies.with_parent`, 1063); "sent to Priya on 5
     Oct, meeting Friday" goes to the extractor and the approval path as before.
5. `kind != ""`: named people (14.4) if any; otherwise the people in the post whose ask
   fits the word: "researched" → ask `researched`; "sent" → `email_out`, `log_date`,
   `dm_out`; "done" → everyone in the post. Exactly one → that person. More than one and
   none named → ask which (14.6), `_remember_said(sent, kind="next_step_which",
   root_id=root, which=[row keys])`, return True. None (a "researched" under a post with
   nobody on research) → fall back to everyone in the post and apply the same rule.
6. Answer with `wording.next_step_done_line(...)` for each picked person, one line each,
   through `self._reply(message, text, reason="next-steps reply")`. Return True.

### 14.4 Names
A person is named when the reply contains, as whole words after
`gtm_sheet.normalise_header`, their full name, their first name, or their company. A
first name shared by two people in the post names neither. `short` in a line is the
first name, or the full name when the first name is shared in the post, or the company
when the row has no name.

### 14.5 replies.py (pure, with self-test lines)
```
NEXT_STEP_DONE = ("done", "all done", "did", "did it", "yes", "yep", "yeah", "yup",
                  "completed", "finished")
NEXT_STEP_RESEARCHED = ("researched", "research done")
NEXT_STEP_SENT = ("sent", "sent it", "went out", "gone out", "emailed")
NEXT_STEP_NOT = ("not", "no", "yet", "havent", "didnt", "wont", "will", "tomorrow",
                 "later", "soon")
NEXT_STEP_MAX_WORDS = 12
def next_step_done(text) -> str
def next_step_all(text) -> bool
```
`next_step_done`: "" when the text has a "?", more than 12 words, or any NEXT_STEP_NOT
word (apostrophes deleted, as `approvals.read_vote` does); else "researched" if a
RESEARCHED phrase is in it, else "sent", else "done", else "". Whole-word phrases.

### 14.6 wording.py: every new string
```
next_step_done_line(ask, *, short, n=0)
  researched       Nice, can you set Next Steps for {short} to Send email 1?
  email_out        Nice, can you mark {nth} Email Sent for {short} and add the date?
  dm_out           Nice, can you log LI DM Sent and the LI DM Date for {short}?
  call_no_dm_date  Nice, can you log the LI DM Date for {short}?
  call             Thanks. If a meeting comes of it, can you add the Meeting Date for {short}?
  dm_replied       Nice, can you add the Meeting Date for {short} once it's booked?
  ask_next         Thanks. Can you pick the next step for {short} in Next Steps?
  advance, log_date   Thanks, I'll pick {short} up from the sheet.
  unresponsive     Thanks, I won't ask about {short} again.
next_step_which(people)   people = ["Priya Rao (Acme Labs)", …], numbered
  Which one?
  1. Priya Rao (Acme Labs)
  2. Dev Shah (Borealis)
  A name or a number is fine.
```
Cell names come from the constants already in wording.py. All added to `all_lines()`
through `next_step_lines_for_check()`. Same rules as 8.2: `register_problems == []`, no
rule number, no schedule words, no setting name, no banned phrase. Wording is Q11.

### 14.7 What does not change
- Rule 13 state: untouched by any reply. The person comes back by rotation until the
  cell changes; when it changes, the signature changes and the next ask follows.
- No sheet read: the line is built from the recorded post. If the cell was already
  updated, the line still asks. Accepted; Q12.
- `_maybe_acknowledge`, `_maybe_vote_on_proposal`, `_maybe_accept_offer`: no edit.
- The F2 guard of 9.3 was replaced by 1063's vote rewrite; its test (G1) stays.
- Test mode and live: identical, tag included. `_reply` does not tag an answer in either
  mode; only proactive posts carry the [TEST…] tag (corrected by the tester). Nothing
  is stored but the in-memory `_said` entry. After a restart a reply to an old "which
  one?" line is an ordinary reply.

### 14.8 Tests (tester; the three SKIPs in verify_rule13.py:812-814 become checks)
Add to tests/test_rule13.py, with fakes that count model calls, sheet writes and votes:
| # | Case | Expect |
|---|---|---|
| Y1 | "done" replied to a post whose only person is on Research the PoC | "Nice, can you set Next Steps for Priya to Send email 1?" |
| Y2 | two research-step people, unnamed "done" | the "Which one?" line naming both; then "2" and, separately, "Dev" each give Dev's line |
| Y3 | "researched" in a post with one research person and four on email steps | that person's line, no question |
| Y4 | "done for Priya" in a five-person post | Priya's line only |
| Y5 | one line per ask code | the exact strings of 14.6 |
| Y6 | every Y case | zero sheet writes, zero proposals opened, zero votes recorded, zero model calls, both Rule 13 tables unchanged |
| Y7 | an approver's "yes" with an email_write proposal open elsewhere | the done line or the "Which one?" line; the proposal still open, no vote |
| Y8 | "sure", "ok", "thanks", a thumbs-up, "not yet" | one reaction, no text, no model call |
| Y9 | "which email template should I use for Priya?" | hook returns False; the engine is called and its question carries the post's text |
| Y10 | "haven't sent it yet", "will do tomorrow" | not a done; no done line |
| Y11 | a reply to a Rule 13 post with no `next_step_posts` row | hook returns False |
| Y12 | SALES_TEST_MODE false and true | the same reply, byte for byte (answers carry no tag) |
| Y13 | "done" by a non-approver teammate | the done line (default d) |
Also `python -m replies` and `python -m wording` gain their lines; tests/test_replies.py
and verify_replies.py pass unchanged.
verify_replay_oct6.py, RULE 13 section: after the post, replay "done", "yes", "sure" and
a question through `on_message`, live and test mode; the printed SKIP goes.
Scripts run with ANTHROPIC_API_KEY set to a fake value. No real-sheet read by anyone.
Never `python -m gtm_sheet`.

### 14.9 Questions
- Q11. The wording in 14.6. [as written]
- Q12. An unnamed "done" under a post with several people asks "Which one?" rather than
  treating it as done for all of them; and the line is sent even if the cell was already
  updated (no sheet read). [as written]
- Q13. "sure" and "ok" under a Rule 13 post are acknowledgements (a reaction), not
  "done". [as written]

## 15. When NFT2-1069 lands

- R13 gets `fixed_time: "15:00"` and a `priority` (1069 makes `priority` required on
  every rule; proposed 2). The evaluator stops setting `dayof_time`; `NEXT_STEP_TIME`
  moves to the RETIRED block ("→ bot_rules.yaml R13 fixed_time").
- Delete `drip.ON_TIME_TYPES` and both guard changes of 7.3: 1069 replaces the global
  catch-up guard with a per-message check for spaced posts only, and fixed-time rows are
  not anchors (1069 section 5.4 and 5.7). Rows R13 wrote before then carry `pinned=1`
  and its rule declares `fixed_time`, so 1069's anchor rule already treats them correctly.
- 1069's `_drip_alarm` sends within a minute of the time, which ends the tick lateness.
- **To raise with the human then:** a fixed 15:00 post is a "block". Spaced posts must
  stay 120 minutes from it, so 14:00 moves to 17:00 and the day holds 15:00 fixed, 17:00
  and 19:00: two spaced posts a day, every weekday (1069 C6). Options then: move R13 to
  12:00 or earlier, or let R13 not be a block.
- 1069's ordering uses the rule NUMBER, not file order; R13's place before R5 in the file
  matters only to the dedup and stays.
- 1069's T17 (a temp `fixed_time` rule) gains a shipped example; its plan line "Rule 13
  is not built" (D8) is out of date.

---

## 16. What must NOT change
- CLAUDE.md hard rules. Rule 13 writes nothing to the sheet, proposes nothing, searches
  nothing, contacts nobody outside the sales channels.
- `write_cells`' three locks, `write_email`'s six checks, `append_row`'s six steps,
  `POC_ROW_ADD_WRITE_WIRED`, the approval gate.
- R1 to R12's evaluators and renderers, except R6's one line. R7's evaluator stays.
- `NEVER_COUNTED`, `PLANNED_FIRST`, `stage_for`, the cap counter, Sunday, the kill switch,
  claim-before-send, one message per tick.
- `row_gate` / `stop_reason`, `activation`, `deadlines.parse_date`.
- NFT2-1062 notes scoping, NFT2-1064 replyguard and the existing wording lines,
  NFT2-1065 search and row add.
- No test that guards a hard rule is weakened.

## 17. Live-channel checklist (test channel first: SALES_TEST_MODE=true and a *_test.db)
1. Boot log: `[gtm.window]` says restricted A:I, Q:W, Z:AE and window J:P, X:Y; the role
   report shows `outreach_step`→'Next Steps', the six email roles, `next_steps`→
   'Notes/Remarks', `poc_priority`→'Priority'; no new-row warning; 13 rules, R7 DISABLED,
   R13 "Mon, Tue, Wed, Thu, Fri, max 5/post, channel, OUTSIDE the cap".
2. "cadence preview": an R13 section with 5 people and the block naming the four rows
   with no LI Connected Date.
3. First weekday at 15:00 (within one sweep tick): one post headed "Next steps", the
   tags line, an opener, up to 5 bullets "Name (Company): …". No rule number, no
   schedule talk. Every name, step and date matches the sheet row. Rows 1 to 5 of the
   Connected list unless a call reminder is due.
4. The 14:00 news post and the other posts go at their usual times.
5. Next weekday: the next 5, nobody repeated. After someone changes a person's Next
   Steps, that person is back in the next post with the new ask.
6. `next_step_followups` has one row per person named; `next_step_posts` one row a day.
7. Reply "done" to the post: Saley may answer or ask which company; the sheet does not
   change; no approval is consumed. Reply "yes" while an email offer is open: the offer
   stays open. (The proper "Nice, can you set Next Steps…" line arrives with NFT2-1063.)
8. Saturday and Sunday: nothing.
9. R9: a completed meeting with Notes/Remarks blank is followed up; one with notes is not.

## 18. Questions for the human (the planner's default in brackets; built that way)
- Q1. `NEW_ROW_WRITABLE_RANGES` is `A:R` and `GTM_COLUMN_MAP` is blank on this laptop,
  not what the brief says. Set the laptop line in section 10; is the server as the brief
  describes? [change the laptop; check the server]
- Q2. The wording of section 8.2 and the heading "Next steps". [as written]
- Q3. R6's post still says "no DM yet" in its heading, its name, its `plain` line and its
  three fallback sentences ("Want to send one this week?"), which contradicts decision 1.
  Reword them to the email check, or change only the item line the ticket names? [only
  the item line and the policy exemplar; the rest waits for wording from Vaishnavi]
- Q4. AF "Priority" sits outside every band. Widen the last band to `Z:AF`? [no change;
  the tier refuses it and B5 tests that]
- Q5. A test day on a non-test database does not record Rule 13's rotation (stricter than
  R9). Or exactly what R9 does, which moves live state? [stricter]
- Q6. The Unresponsive reminder ends Rule 13 for that person for good, whatever the cells
  later say. Or should a later change to Next Steps reopen them? [for good; the preview
  lists them]
- Q7. `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=150` on the laptop makes "15:00" up to two and
  a half hours late. Set 15? [yes, in both .env files]
- Q8. The scripts that stay red (13.5): in scope here? [no]
- Q9. R13 is listed before R5, so on a day R13 names someone, R5, R6 and R10 skip that
  person. Acceptable, or should Rule 13 be allowed to name a person another rule also
  names that day? [R13 first, one mention a day]
- Q10. Default f detail: a "Replied" contact is asked about the meeting from the first
  post, in the ordinary rotation, with no end. [as stated]
