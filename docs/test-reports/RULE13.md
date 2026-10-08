# RULE 13 test report: Next Steps follow-ups and the 7 Oct 2026 sheet layout

Tester: tester-r13. Written from docs/plans/RULE13.md and the ticket before the builder's diff was read; the diff was read
afterwards and the extra checks it prompted are listed in section 4. Tree: HEAD 7ff8eb0 plus the Rule 13 work plus, from
partway through my runs, NFT2-1063 work landing in the same working tree (section 6). Real-sheet reads: see section 2.

## 0. Verdict

- Rule 13 and the 7 Oct layout: every check I wrote passes (tests/test_rule13.py 187 tests, verify_rule13.py 61 checks and
  3 SKIPs, the Rule 13 section of verify_replay_oct6.py, all existing-test updates green).
- Baseline: every script that was red before is red on the same lines after, except three that improved
  (`python -m sheetwrite` 3 -> 0 failures, `verify_parity --fixture-only` loses a line that is an artefact of the baseline copy,
  verify_s1 unchanged) and the lines NFT2-1063 moved (section 6).
- NOT run: the final dry run against the real sheet (section 7). One real-sheet read was made by mistake (section 2).
- One finding was sent to the builder during the run and fixed (section 5, F1).
- Replies were deferred, then built (plan section 14) and tested: see section 14 ("Replies"). Sections 3 and 9 describe the state before that.

## 1. Files written or changed

Written (new): `tests/rule13_fixtures.py` (made-up people on the real 7 Oct header row, the 33-row / 29-dated / 18-11-4
rotation world), `tests/test_rule13.py` (169 tests), `verify_rule13.py` (bot-level checks, the printed tables, the
`--real-sheet --date` dry-run mode), this report.

Extended: `verify_replay_oct6.py` (the shared harness: a Rule 13 section after the profile section, a header-docstring
entry, and a stdout utf-8 reconfigure so a FAIL line quoting Gemini punctuation cannot crash it. Steps 1, 5, 8, 9 untouched).

Existing tests updated (each legitimate, none weakened; the plan section in brackets):

| # | File | Old | New | Why |
|---|---|---|---|---|
| 1 | tests/conftest.py | `POCS_HEADERS` = the 24-column A-X row; three fixture rows hard-coded to 24 cells | `POCS_HEADERS` = the 7 Oct row (32 columns, A-AF); the old row kept as `POCS_HEADERS_PRE_7OCT`; rows built to `len(POCS_HEADERS)` | [13.4] the sheet grew columns Q-W and AF |
| 2 | tests/canned_sheet.py | same old row; rows already built by index | the 7 Oct row, docstring says so | [13.4] same; the canned rows use indexes 0-6, unchanged |
| 3 | tests/test_signed_row_append.py:109 | `NEW_ROW_WRITABLE_RANGES` "A:R" | "A:P,X:Y" | [13.4] the new-row band; the closure refusal (248) and `A:H` (294) stay as they were |
| 4 | tests/test_regressions.py:385 | docstring "S-X never is" | "Q-W and Z-AE ... never are"; the assertions (closure_prob refused, company written) are unchanged | [13.4] |
| 5 | tests/test_regressions.py:666 | `len(words) == 12` | `== 13` | [13.4] R13 added; ids never renumbered |
| 6 | verify_approvals.py | bands pinned "A:I,S:X"; the 24-column header row and row | pinned "A:I,Q:W,Z:AE"; the 7 Oct header row, row padded to 32 | [13.4] every check kept; Meeting Date is now X, still in the window |
| 7 | verify_s3.py (ii) | R7 on a Monday through the live sweep, against the shipped rules | the same check against a TEMP COPY of bot_rules.yaml with only R7 switched on; plus a new check that, as shipped, the same Monday posts no R7 | [13.4] R7 is `enabled: false` as shipped (rule 13 supersedes it); the evaluator and renderer are kept and still proved |
| 8 | verify_s3.py (w) | strategy must contain "+N more next Monday" for rules 3, 4, 5, 7, 10 | the phrase dropped for rule 7; new check that the strategy says "Replaced by rule 13" and no longer carries R7's overflow line | the strategy now says rule 7 is replaced |
| 9 | verify_s3.py header row (178) | old pre-7 Oct row | KEPT (plan: "it becomes the pre-7 Oct check") | note: verify_s3 therefore does not exercise the 7 Oct layout |
| 10 | verify_s1.py | Monday with 6 countable groups relied on R7 | the whole script runs against a temp copy of the rules with R7 on (comment says why); NEW checks run the same Monday against the shipped rules: 7 groups, no R7, 5 counted, R8 and R9 outside the cap, nothing rolled | [13.4] the cap-roll scenario needs six counted groups; both are now shown |
| 11 | verify_s1.py (w) | Global Rules "Daily cap" sentence without next-step follow-ups | the new agreed sentence from bot_rules.yaml | [8.1] `global_rules.daily_cap` updated |
| 12 | verify_parity.py | same cap sentence | the new sentence | [13.4]/[8.1] |

Not changed: verify_research_timing, verify_news_feed, verify_search_backend (read the canned sheet; unaffected by the
layout), verify_layouts (never run), verify_monday, verify_heavy_monday, verify_tone, verify_voice, verify_voice_profile,
verify_testday, verify_testday_talk (hand-built items passed to `drip.plan`).

## 2. Commands, results, and the real-sheet count

Real-sheet reads so far: **1, by mistake, none intended.** `python -m gtm_sheet` is listed in the plan's 13.4 table as a
module self-test; it is in fact a LIVE access check. Run without `--write` it is read-only (`SHEETS.check_access()` plus
role/schema reads: the spreadsheet metadata and one values batch). Nothing was written or posted and I printed only its first
lines (playbook title, "24 tabs"). I told the lead at once. `-m gtm_sheet` is not in any sweep. Everything else was either
offline-guarded (the guard line is in the table) or pure.

All sweeps ran under the `.env.agent` values (secrets forced to the sentinel "x"), once on a `git archive HEAD` copy (before)
and once on the tree (after), per script, offline modes only. The HEAD copy has no gitignored database, which explains one
baseline-only line below.

| Command | Result | Offline-guard line |
|---|---|---|
| `python -m py_compile` on every changed .py | OK | n/a |
| `python -m pytest -q tests/test_rule13.py` | 169 passed | autouse via conftest |
| `python verify_rule13.py` | ALL PASSED (61 checks, 3 SKIP deferred) | clean |
| `python verify_replay_oct6.py` | the Rule 13 section: all PASS (see section 6 for steps 1) | clean |
| `python verify_s3.py` | ALL PASSED | clean |
| `python verify_approvals.py` | ALL PASSED | clean |
| `python verify_s1.py --no-ddg` | 1 FAILED: the baseline "[TEST] tag" line | clean |
| `python verify_parity.py --fixture-only` | 1 FAILED: the baseline "[TEST] tag" line | clean |
| `python -m rules`, `nextaction`, `wording`, `sheetwrite`, `approvals`, `replyguard`, `toolsets`, `focus`, `websearch`, `tone` | all ALL PASSED | n/a |
| `python drip.py` | 6 FAIL lines, the same six as before | n/a |

Full before/after table: section 8.

## 3. Results by group (tests/test_rule13.py unless marked)

| Group | What | Result |
|---|---|---|
| H1-H4 | both header rows, every role on its column, Q never the notes role and Z never the step role (also through GTM_COLUMN_MAP, in both directions), stop-gap blank-or-set is identical, new roles in NEXT_ACTION_ROLES and not in CADENCE_ROLES, R9 reads Z (blank Z follows up, notes silence it), R9 on the pre-7 Oct row reads S | PASS |
| B1-B6 | contract defaults in .env.example and in code (blank variables give A:I,Q:W,Z:AE and A:P,X:Y), window J:P, X:Y, the `[gtm.window]` log line names J..P, X, Y and no restricted column, writes to Q R W Z AE refused with no request reaching the worksheet, J P X Y writable, new-row band, `append_row` refuses the step/notes/closure roles, `write_cells_on` refuses the Outreach PoCs tab, the eight new roles are in no tier for either trigger, `plan_writes` turns each into an ask, the startup warning names Q:R for A:R and follows the configured bands (S:T against S:X), `email_write` writes only F | PASS |
| T1-T11 | one test per table row with every listed edge: due dates found by scanning, not by copying the code's arithmetic; blank previous dates, DD.MM.YYYY text, `Send Email 1`/`Call PoC` forms, an unknown Q (skipped, logged, listed), Q disagreeing with the cells | PASS |
| C1-C5 | call clock on days 7, 10, 14, 17, 21 and Unresponsive on day 22, never again (even after Q changes), a missed post does not lose the reminder, numbers read from config, dm->call mid-clock adds nothing and the count continues, Prospect Status Unresponsive stops everything, defaults f and g with their switches | PASS |
| R1-R5 | the 33/29 world over 10 weekdays (printed below), 5 a post, nobody twice before everyone once, wrap to the top, call first, step change clears state, 4 undated skipped and logged each day, nobody due = nothing and no state row, unreadable state = nothing | PASS |
| D1 | Tuesday, shipped rules: R13 keeps the contact, R6's copy is in `deduped` | PASS |
| O1 | 13 rules in file order, R13 before R5, R7 off and silent on a Monday, evaluator kept, R6 text, daily cap text, the plan's per-day rule lists | PASS |
| P1-P2 | one pinned 15:00 post outside the cap, never held two days running, a cap of 0 does not roll it, not addressed to the deliverables owner, spaced posts keep their times, the time is a setting, Saturday and Sunday silent | PASS |
| P3 (verify_rule13) | a spaced post sent 5 minutes earlier does not hold the 15:00 post; the R13 row is not the "last post" that delays the next spaced one | PASS |
| S1-S4 (verify_rule13) | state only after a real send (5 people + 1 posts row), a send the guardrails refuse records nothing, the queue and preview read twice leave both tables byte-identical, test day on a non-`_test.db` records nothing and logs why, with `_test.db` records, with `bot.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB=True` records, simulation leaves the real file unchanged | PASS |
| S5 / parity (verify_rule13, replay) | live, SALES_TEST_MODE, test day, simulation: the same post apart from the tag; 15:00; zero model calls and zero searches | PASS |
| V1 | every line passes `register_problems`, no banned phrase, no rule number, no schedule word, no setting name | PASS |
| G1, G2 (verify_rule13, replay) | "yes" from both approvers and "done"/"sent"/"researched" replied to a Rule 13 post with an unrelated cell-update proposal open: no vote, proposal still open, zero sheet writes, no Rule 13 state changed; control: a "yes" to the proposal's own message does vote | PASS |

The ten-weekday rotation as printed by verify_rule13 (people numbered 1..29 in sheet order; 4 undated rows skipped every day):

```
Mon 12 Oct  [1, 2, 3, 4, 5]        Mon 19 Oct  [26, 27, 28, 29, 1]
Tue 13 Oct  [6, 7, 8, 9, 10]       Tue 20 Oct  [2, 3, 4, 5, 6]
Wed 14 Oct  [11, 12, 13, 14, 15]   Wed 21 Oct  [7, 8, 9, 10, 11]
Thu 15 Oct  [16, 17, 18, 19, 20]   Thu 22 Oct  [12, 13, 14, 15, 16]
Fri 16 Oct  [21, 22, 23, 24, 25]   Fri 23 Oct  [17, 18, 19, 20, 21]
```

The call clock (DM on Mon 21 Sep, a post every weekday): call on days 7, 10, 14, 17, 21, the Unresponsive reminder on day 22, nothing
after.

One post, live, as Discord shows it (the same text in test mode, on a test day and in a simulation apart from the `[TEST]` tag):

```
**Next steps**
@Vaishnavi
Where a few of our connections stand:
• Person 01 (Acme Labs 01): Next Steps says Send email 2. Has it gone out? If so, mark 2nd Email Sent and the date.
• Person 02 (Acme Labs 02): Next Steps says Research the PoC. Have they been researched? If so, set Next Steps to Send email 1.
...
```

The preview block: `R13 · who is not in today's post — 33 connected, 29 due, 5 picked, 0 not due yet, 0 with a completed meeting`, then the
queued people and the four rows with no LI Connected Date by name.

## 4. Edge cases covered, and what the builder's diff added to my list

Ticket edge cases: blank previous date, DD.MM.YYYY dates (also a two-digit year, which is "no readable date"), normalised Q values,
an unknown Q, Q disagreeing with the cells, "Not connected" / "Disconnected" / "connected?" rows (exact match only), stopped and
snoozed rows at every step, defaults f and g with their switches, the 4 undated rows, a closed person after a Q change, a missed day.

Added after reading the diff (not in the plan): the recomputed-every-tick behaviour (once today's post has gone, the same five
come back until tomorrow; a stopped person is not replayed), `nextaction.NEXT_STEP_CLOSED_IS_FINAL` (shipped True, and with it off a
step change reopens a closed person), `bot.NEXT_STEP_TEST_DAY_RECORDS_ON_LIVE_DB` (shipped False), a row with no name is named by its
company alone, the opener cycles over three days and is not random, the call reminder stays first after `drip.group` re-sorts,
`NEXT_STEP_TIME` falling back on an unreadable value, the call-window and empty-marker warnings, and the docs/rule13-for-the-sheet.md
cells equal to what bot_rules.yaml loads.

## 5. Findings

- **F1 (sent to builder-r13, fixed): R13 asked "Next Steps is blank" about everyone when no Next Steps column was mapped** (the
  pre-7 Oct header row, or a renamed Q). That states something false about the sheet. Now: nothing is posted, the report state is
  `no_step_column`, a WARNING names the unmapped roles. Pinned in `TestUnmappedColumns` (pre-7 Oct row, a renamed email column, and
  the 7 Oct row still fine).
- **F2 (plan gap, not a failure): the plan's test table puts S1-S5 and G1-G2 in tests/test_rule13.py.** They need a fake Discord, so
  they are in verify_rule13.py and the replay harness.
- **F3: `python -m gtm_sheet` is a live sheet access check** (section 2). The plan's 13.4 table lists it as a self-test. The plan
  should say so.
- **F4: R5 and R6 lose any contact R13 names on the same day** (plan Q9, built as the default). In the printed preview the
  contacts R13 picked are listed as "R5 also selected this contact". Working as designed; the human should know it.

## 6. The tree moved under me: NFT2-1063 in flight

Midway through my runs another builder's NFT2-1063 work (replies.py, toolsets.py `todays_objectives`, a rewritten
`_maybe_vote_on_proposal`, tests/test_replies.py) appeared in the same working tree. Consequences for the numbers:

- pytest, verify_profile_lookup, verify_notes_scope fail 5 / 1 / 4 more lines after than before. Every one is the `today` tool group
  now offering `['show_todos', 'todays_objectives']` (and the frozen prompt hash that includes every tool name). I proved the pytest
  five go away when toolsets.py alone is put back to HEAD (298 passed). Not Rule 13.
- verify_replay_oct6 step 1 (NFT2-1062's, which I did not touch) now fails 6 lines for the same reason. Whoever finishes 1063 must update
  step 1's "exactly show_todos" expectation in the shared harness.
- tests/test_replies.py had 42 failures in one run. Not mine.
- 1063's rewrite removed the "newest open proposal" fallback for every post, so the plan's F2 hole is closed more broadly than the
  Rule 13-only guard the builder added. My G1 checks assert the invariant (a reply to a Rule 13 post votes on nothing, writes nothing),
  which holds under either guard; the control was changed from "a yes to a message with no proposal votes" (no longer true) to "a yes
  replied to the proposal's own message votes".
- A pytest run caught test_env_example failing once while 1063 added a variable; it passed on the next run.

The Rule 13 results above were re-run after 1063 landed; my files pass.

## 7. The final dry run (NOT RUN)

The brief allowed ONE real-sheet read in the whole session, for the Rule 13 preview for Thursday 8 Oct. I spent it by mistake
(section 2), so the dry run would be a second read. I did not run it and told the lead; I will run it only on an explicit "go". The command,
as I would give it to the lead:

```
python verify_rule13.py --real-sheet --date 2026-10-08
```

Why it is one read, no write, no post, no state change: (1) `gtm_sheet.SHEETS.read` is wrapped with a counter that raises on a
second call; (2) `write_cells`, `write_cells_on`, `write_email`, `append_row`, `undo_cells`, `clear_cells` are replaced with
functions that raise; (3) Rule 13 is evaluated by `nextaction.run(day_rules=[R13], next_step_state={})`: the state is the literal `{}`,
so no database is opened and nothing is recorded; (4) it never imports or builds the bot, so there is no Discord client and no model
client; (5) it prints `reads=1 posted=0 written=0 database opened=none model calls=0`. On any error it prints the error class and stops with no
retry. The mode was REHEARSED offline with the one read replaced by the made-up fixture (reads=1, five people printed with their lines,
exit 0, offline guard clean), so a typo will not be what the real run finds. It prints the header row, the Connected/dated/undated counts (expect 33 / 29 / 4) and the step breakdown over all 33 Connected rows (expect 18 / 11 / 4), the 5
people with each one's line, and the report lists (queued, unknown step values, rows with no date).

Result for the report: **not run; awaiting the lead's decision.** The expected-against-the-ticket counts, the five people and their lines,
and the four undated rows would go here.

## 8. Baseline table (before = `git archive HEAD` under `.env.agent`; after = tree). FAIL lines = lines containing FAIL

| Script | Before (exit / FAIL lines) | After | Same lines? |
|---|---|---|---|
| pytest -q tests/ | 1 / 1 (TestPostingWindow) | 1 / 6 | the 5 new are NFT2-1063 toolsets (section 6) |
| verify_answer_voice, verify_replay_oct6 (Rule 13 + steps 5, 8, 9), verify_interim, verify_news_question, verify_llm_audit, verify_tokens, verify_approvals, verify_news_events, verify_news_feed, verify_research_timing, verify_points, verify_s3, verify_tone, verify_voice, verify_voice_profile | green | green | step 1 of the replay fails after 1063 landed (6 lines, section 6) |
| verify_profile_lookup --as-shipped | green | 1 FAIL | 1063 (T4 offers show_todos + todays_objectives) |
| verify_notes_scope | green | 4 FAIL | 1063 (same cause) |
| verify_monday / verify_heavy_monday | 4 / 4 | 4 / 4 | same |
| verify_clock | 5 | 5 | same (only the clock time in the text differs) |
| verify_testday / verify_testday_talk | 2 / 1 | 2 / 1 | same |
| verify_test_output | 8 | 8 | same |
| verify_reminders | 5 | 5 | same (clock times only) |
| verify_s1 --no-ddg | 1 | 1 | same: the "[TEST] tag" line. R13 touched it: R7 off so the Monday has 7 groups, handled by the temp-rules approach plus an as-shipped check; the cap sentence updated |
| verify_s2 | 1 | 1 | same |
| verify_parity --fixture-only | 2 | 1 (after my cap-sentence update) | the tag line stays; the baseline's "a voice profile is stored" line fails only in the archive copy (no gitignored DB) |
| verify_websearch --offline | crash KeyError | same | same |
| verify_search_backend --stub | 12 | 12 | same; Rule 13 does not touch it (canned sheet) |
| `python drip.py` | 6 | 6 | same six (laptop gap values); +13 PASS checks for R13 (plan, 15:00, outside the cap, never held, verbatim, order, opener, guard exemption) |
| `python -m sheetwrite` | 3 FAIL | 0 | improved: the self-test now uses the 7 Oct row the laptop's bands already assume |
| `python -m rules`, `nextaction`, `wording`, `approvals`, `replyguard`, `toolsets`, `focus`, `websearch`, `tone` | green | green | more checks, all pass (rules 26->32, nextaction 88->141, wording 50->187 PASS) |

`verify_interim` prints an `APIConnectionError` line in both trees (it builds a real client with the sentinel key; no network here); exit 0 both ways.

## 9. Not run, deferred, and why

Deferred to NFT2-1063 (listed, not written as passing tests): "done" to a Research the PoC line gets the Send email 1 reminder; two
research people and an unnamed "done" asks which; a real question in a reply is answered with the post as context; "consistent with
how NFT2-1069 handles fixed-time posts". What is tested today instead: a "done" and an approver's "yes" replied to a Rule 13 post write
nothing, apply no open proposal, change no Rule 13 state (G1, G2), and the post's people/step/ask/signature are stored after a real send
(S1) for 1063 to read.

Not run: `python -m gtm_sheet` (live), verify_poc_lookup, verify_layouts, verify_simulation, verify_search_backend without `--stub`,
verify_parity without `--fixture-only`, verify_websearch/tone/voice/voice_profile/tokens/news_events/s2 in their live modes,
tools/tone_samples.py --live-model, anything that calls the model or the network. verify_layouts was not edited either (plan: only
if Q3 is yes, and Q3's default is no).

## 10. Defaults a-g: awaiting Vaishnavi (each built as a default, each tested against the setting, not a copy)

| # | Default built | Changed in | Test |
|---|---|---|---|
| a | weekdays only, 15:00 IST, Saturday and Sunday silent | bot_rules.yaml R13 `weekdays`; `NEXT_STEP_TIME` | O1 (days), P1 (time is a setting), P2 |
| b | outside the daily cap | bot_rules.yaml R13 `counts_toward_cap` | P1 (`TRIGGER not in NEVER_COUNTED`, the rule decides) |
| c | remind only; Saley never writes Q-W | RESTRICTED_COLUMN_RANGES holds the lock | B2, B4, B5 |
| d | "VR" is Vaishnavi; a "done" from any teammate counts | nothing built (replies deferred) | deferred |
| e | sheet order; Priority is read, never used for order | code constant | `test_priority_never_changes_the_order` |
| f | LI DM Sent = "Replied": no call chase, asks about the meeting | `NEXT_STEP_DM_REPLIED_MARKERS` | C4, T6 |
| g | booked future meeting pauses; past not Completed carries on; Completed ends it | `NEXT_STEP_PAUSE_FOR_BOOKED_MEETING` | C5 |

## 11. Values pinned by the tests, and the laptop's real ones where they differ

Pinned in my tests (never read from `.env`): every `NEXT_STEP_*` at its `.env.example` default; bands `A:I,Q:W,Z:AE` and `A:P,X:Y`;
`SIMULATION_PREFIX=[TEST]`; sales channel 4343 and test channel 4242 (fake); `DAILY_MESSAGE_CAP=5`; window 14:00-18:30, gap 90/15/30;
`EMAIL_WRITE_ALLOWED=false`; the date fixed at Mon 12 Oct to Fri 23 Oct 2026.

The laptop's real values that differ: `NEW_ROW_WRITABLE_RANGES=A:R` (the new startup warning fires on this laptop and names Q:R; my B6 test
proves it), `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=150` (the 15:00 post can land up to 149 minutes late), gap values 120/120/120 and
`DAILY_MESSAGE_CAP=15` (why TestPostingWindow, verify_monday and verify_heavy_monday are red), `SIMULATION_PREFIX=[TEST-live]` (why the
tag line is red), `EMAIL_WRITE_ALLOWED=true`, `GTM_COLUMN_MAP` blank.

## 12. NEW / CHANGED-default / RETIRED env variables, and the lines for both .env files

NEW (all in .env.example, uncommented, defaults shown): `NEXT_STEP_TIME=15:00`, `NEXT_STEP_CONNECTED_MARKERS=connected`,
`NEXT_STEP_FIRST_DAYS=2`, `NEXT_STEP_AFTER_PREVIOUS_DAYS=2`, `NEXT_STEP_AFTER_EMAIL_DAYS=7`, `NEXT_STEP_CALL_AFTER_DM_DAYS=7`,
`NEXT_STEP_CALL_EVERY_DAYS=3`, `NEXT_STEP_CALL_UNTIL_DAYS=21`, `NEXT_STEP_DM_REPLIED_MARKERS=replied,responded`,
`NEXT_STEP_PAUSE_FOR_BOOKED_MEETING=true`. CHANGED default: `RESTRICTED_COLUMN_RANGES` `A:I,S:X` -> `A:I,Q:W,Z:AE`;
`NEW_ROW_WRITABLE_RANGES` `A:R` -> `A:P,X:Y`. RETIRED: none.

Laptop `.env` (agents do not edit it):
```
NEW_ROW_WRITABLE_RANGES=A:P,X:Y          # is A:R today; must change
RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE    # already set
GTM_COLUMN_MAP=                          # already blank; leave blank
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=15   # recommended (is 150): "15:00" is otherwise up to 2.5 hours late
```
Server `/opt/sales-bot/.env`:
```
RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE
NEW_ROW_WRITABLE_RANGES=A:P,X:Y
GTM_COLUMN_MAP=                          # blank the stop-gap {"outreach_pocs":{"next_steps":"Notes/Remarks"}} if it is set
# confirm COS_FOLLOWUP_CHECK_INTERVAL_MINUTES is 15 or unset
```
The ten NEW lines are optional: the defaults apply without them. Then `python tools/redact_env.py` on the laptop.

## 13. LIVE-CHANNEL CHECKLIST (for a human; test channel first: SALES_TEST_MODE=true and a `*_test.db`)

1. At boot, read the log: `[gtm.window]` says restricted `A:I, Q:W, Z:AE` and window `J:P, X:Y`; the role report shows `outreach_step`
   -> "Next Steps", the six email roles, `next_steps` -> "Notes/Remarks", `poc_priority` -> "Priority"; no "reaches into restricted
   column" warning; 13 rules, R7 DISABLED, R13 "Mon, Tue, Wed, Thu, Fri, max 5/post, channel, OUTSIDE the cap".
2. In the test channel type `cadence preview`. Expect an R13 section with 5 people, then "R13 · who is not in today's post" listing
   the four Connected rows with no LI Connected Date by name and sheet row.
3. On the first weekday at 15:00 IST (within one sweep tick of it): one post headed **Next steps**, a tags line, one opener line,
   up to 5 bullets "Name (Company): ...". No rule number, no schedule talk, no URL. Every name, step and date matches its sheet
   row. The people are the first five dated Connected rows, unless a call reminder is due, which goes first.
4. The 14:00 news post and the other posts go at their usual times, unaffected.
5. The next weekday: the next five, nobody repeated. After you change one person's Next Steps in the sheet, that person is in the
   next post with the new ask.
6. `sqlite3 <test db> "select count(*) from next_step_followups; select count(*) from next_step_posts"`: one row per person named,
   one posts row per day.
7. Saturday and Sunday: no Rule 13 post.
8. Rule 9: a Completed meeting with Notes/Remarks blank is followed up; one with notes is not.
9. Type `make it Monday` on a database that is NOT `*_test.db`: the post appears but the log says "test day on a live database;
   rotation not recorded", and the tables do not change.
10. **DEFERRED behaviour (NFT2-1063), not expected to work yet:** reply `done` to a line about a Research the PoC person. Today the
    sheet does not change, no approval is used, no Rule 13 state moves, and you may get an ordinary answer or an ack. When 1063 lands the
    reply should be "Nice, can you set Next Steps for <name> to Send email 1?". Also reply `yes` to the post while an unrelated offer is open:
    the offer must stay open.

## 14. Replies (plan section 14, tested after the builder landed the hook)

Written from plan 14.8 before the hook existed (the first run showed the stub: a "done" fell through to the engine and got
"Here is what I have."). Y1-Y13 are `TestRepliesY` in tests/test_rule13.py, on `tests/replies_world.py` (a whole bot behind fake
Discord that counts model calls, sheet writes, votes, proposals and reactions; the post goes out through the real planner and the
real `_send_drip_message`). The three SKIPs in verify_rule13.py are now checks (section (R)), and the SKIP in the replay's Rule 13
section is gone.

| Case | Result |
|---|---|
| Y1 "done" under a one-person research post | "Nice, can you set Next Steps for Priya to Send email 1?" PASS |
| Y2 two research people, unnamed "done" | the numbered "Which one?" line; then "2", "Dev", "dev shah" and "Borealis" each give Dev's line; "both" gives both. PASS |
| Y3 "researched" in a post with one research person and four on email steps | Priya's line, no question. PASS |
| Y4 "done for Priya" / "done, Priya Rao" / "done for Acme Labs" in a five-person post; "sent for Dev" | only that person's line (the email line for Dev). PASS |
| Y5 one exact string per ask code | all twelve strings of 14.6 equal; every one passes register, no rule number, no schedule word, no banned phrase. PASS |
| Y6 zero sheet writes, zero proposals opened or moved, zero votes, both Rule 13 tables unchanged, zero model calls (model calls only exempted for the question) | asserted for 13 different replies in five-person posts, and with the model count included for done, yes, sure, researched, a named done, a thumbs-up and "not yet". PASS |
| Y7 an approver's "yes" with an email_write proposal open elsewhere | the done line (one person) or the "Which one?" line (two); the proposal stays open, no vote. PASS |
| Y8 "sure", "ok", "thanks", a thumbs-up, "not yet" | one reaction each, no text, no model call. PASS |
| Y9 "which email template should I use for Priya?" | the engine is called and its request carries the post's text ("Next Steps says"); no done line. PASS |
| Y10 "haven't sent it yet", "will do tomorrow", "didn't get to it", "not done" | no done line. PASS |
| Y11 a reply with no `next_step_posts` row | no done line (the hook steps aside). PASS |
| Y12 SALES_TEST_MODE false and true | identical, including the tag: see the plan inaccuracy below. PASS |
| Y13 a non-approver teammate's "done" | the done line (default d). PASS |
| extras | a shared first name ("Priya Rao" and "Priya Nair") asks which and the full name picks; the pure `replies.next_step_done` / `next_step_all` helpers. PASS |

Other results: `verify_rule13.py` section (R): the one-person line, the numbered question, "2" and "Dev" each pick Dev, a question reaches the
engine with the post, zero writes and votes, live == test mode: all PASS (76 checks, no SKIPs). `verify_replay_oct6.py` Rule 13 section: "done" and
"yes" under a five-person post both ask "Which one?" with no model call and no vote, "sure" gets one reaction and no text, a real question
reaches the engine, zero sheet writes, the unrelated proposal stays open, no Rule 13 state moved, test mode == live: ALL PASSED (the whole
file, steps 1, 5, 8, 9 included, is green again now that 1063's own step 1 expectations were updated). `python -m replies`, `python -m wording`,
`python -m rules` ALL PASSED; `verify_replies.py` ALL PASSED; full `python -m pytest -q tests/`: 1 failed (the baseline TestPostingWindow), 874 passed,
25 skipped. No real-sheet read was made in this round and `python -m gtm_sheet` was not run; the scripts ran with the sentinel key "x".

Plan inaccuracy found: plan 14.7 says the reply "goes through `_reply`, which tags it". It does not: `_reply` never tags an answer, in
either mode (only proactive posts carry `[TEST...]`). So a reply is identical in live and test mode, tag included. Y12 asserts exactly that. If the
human wants test-mode replies tagged, that is a change to `_reply` for every answer, not a Rule 13 matter.

Pinned behaviour worth knowing: under a Rule 13 post an approver's "yes" is read as "done" (it asks which person or gives the update line) and
never as a vote; a bare "sure"/"ok"/"thanks"/thumbs-up/"not yet" is a reaction only.

Live-channel checklist item 10 is now live behaviour: reply `done` to a Research the PoC line in a one-person post and expect "Nice, can you
set Next Steps for <first name> to Send email 1?"; in a post with several people expect "Which one?" with a numbered list, and answer with a number or
a name. The sheet must not change and no approval is consumed.

### 14.1 Stability: the Y12 report, the verify_rule13 traceback, and three rounds on an unchanged tree (8 Oct)

- **The verify_rule13 traceback was mine, mid-edit.** Adding section (R) I hit two bugs of my own in a row (a nested `asyncio.run` inside the
  running loop, then a second Rule 13 post in one World on the same day being refused by the one-post-a-day slot claim, an IndexError). Both fixed
  (one World per phase, the section runs on a thread). Not a race in the product.
- **Y12:** I cannot reproduce the full-run failure. It never failed in three full runs. The one cause I can evidence is that, when first written,
  Y12 asserted that test-mode replies carry `[TEST]` (the plan said so), which is false, and it failed on that assertion until I changed it to "identical,
  tag included" (the product is right; `_reply` tags nothing). A run made in that window fails full and alone alike, but builder-r13 saw it pass alone,
  so I do not claim that is the whole story. Y12 shares nothing between its two Worlds that could differ (a fresh temp DB per World, a re-seeded `tone.RNG`,
  the same fake date, no wall clock in the compared text). I changed its assertion to print both outputs (`LIVE ... != TEST ...`) so any recurrence carries the
  two differing texts.
- **Three rounds, tree unchanged** (md5 of bot.py, drip.py, nextaction.py, replies.py, wording.py, db.py, tests/test_rule13.py, verify_rule13.py,
  verify_replay_oct6.py identical before and after), `ANTHROPIC_API_KEY` a fake value, no real sheet:

| Round | pytest -q tests/ | verify_rule13 | verify_replies | verify_replay_oct6 |
|---|---|---|---|---|
| 1 | 1 failed (baseline TestPostingWindow), 874 passed, 25 skipped | ALL PASSED | ALL PASSED | 1 FAILED |
| 2 | same | ALL PASSED | ALL PASSED | 1 FAILED |
| 3 | same | ALL PASSED | ALL PASSED | 1 FAILED |

  Y12 and all of `TestRepliesY` passed in all three full runs.
- **The one replay failure is not Rule 13.** It is the same line every time, three more times standalone, in NFT2-1063's section of the file
  (`tests/replay_1063.py`): `7 Oct: the answer came from todays_news (5 stories): got False, want True`. Every Rule 13 line, and steps 1, 5, 8 and 9, pass. It
  appeared after the clock rolled over to 8 Oct (the replay reads `todays_news`, which reports news by the current date); that is my reading, not something I
  proved, and it belongs to whoever owns 1063's replay section. It passed on 7 Oct.
