# R11 Outreach PoCs cross-check — test report

9 Oct 2026. Plan: `docs/plans/R11-POC-CROSSCHECK.md`. One session. Everything was run OFFLINE with
`ANTHROPIC_API_KEY=x`: no model, no network, no real Sheets or Drive (`tests/offline_guard.py`). **Nothing has been
checked in the live channel, and nothing has been run against the real sheet.**

The work was committed by the human as `b379947` while the last test run was in progress. One line changed after
that commit and is uncommitted: `wording.POC_ADD_ALL_OR_NONE` ("I have not" → "I haven't"), which is what made the
one failing test pass.

## 1. Results

| Command | Result |
|---|---|
| `python verify_poc_crosscheck.py` (new, 62 checks) | ALL PASSED |
| `python -m pytest -q tests/` | 920 passed, 25 skipped, **1 failed**: the wording lint on my new line (`test_every_constant_passes_the_register_lint`, "I have not"). Fixed; that test re-run: passed. The full suite was not run a second time |
| `python verify_websearch.py --offline` | ALL PASSED (it crashed with `KeyError: 'new_pipeline_company'` before) |
| `python verify_day_order.py` (runs `tests/day_order_checks.py`) | ALL PASSED |
| `python -m poc_crosscheck`, `-m nextaction`, `-m rules`, `-m websearch`, `-m wording`, `python drip.py` | ALL PASSED |
| `python -m pytest -q tests/test_env_example.py` | 8 passed (the three new variables are in `.env.example`) |
| `python -m py_compile` on every changed file | clean |
| `verify_poc_lookup.py`, `verify_layouts.py` | **NOT RUN.** Both were on the list. They make live web searches and live model calls and have no offline mode. Say if you want them run with a real key |
| the rest of the verify sweep (`verify_s1/s2/s3.py`, `verify_rule13.py`, `verify_replies.py` and others) | **not run** for this change |

## 2. What the new checks show

| Asked for | Result |
|---|---|
| A Wednesday with nothing in scope posts nothing | PASS: no R11 message is planned and nothing is posted |
| Duplicate Master Pipeline rows give one M1 line | PASS |
| Branch B carries the suffix; Branch A does not; complete is in no message | PASS |
| Only optional fields blank is complete, not Branch B | PASS |
| No search before the first yes; no write before the second | PASS: zero search and email-lookup calls after the post; zero writes after M2, with a `row_add` proposal open |
| An omitted field prints nothing; the note appears once, and not at all when everything was found | PASS |
| Branch B never writes Industry, Based, Research Paper Link or LI Url, with `EMAIL_WRITE_ALLOWED` off and on | PASS: no proposal is opened and a "yes" and a "sure" to it write nothing |
| Email-only gap with the switch off: the plain request, no offer | PASS. With it on: the offer line, an `email_write` proposal, and a yes writes the Email cell only |
| A new row writes A, B, C, D, E, G, H, I, is signed, and is refused when it cannot be signed | PASS at the bot level: the values handed to `append_row` are company, industry, name, designation, based, paper link and LinkedIn URL, with `note_role="name"`, the signature text and `fill_serial=True`; those roles are columns B, C, D, E, G, H, I of the 7 Oct layout and all pass `config.may_write_new_row_column`; with no approval link nothing is appended. **`append_row` itself is a recorder here**: its own note-placement refusal is covered by the existing append tests, not by this script |
| `li_url` is blanked for a non-profile link | PASS (a `linkedin.com/company/...` link) |
| A person added between the search and the yes is skipped, and M3 says so | PASS: "Skipped Ross Harper: already on Outreach PoCs." |
| `read_vote("yes to 1 and 2 but not 3")` is no, and nothing is written | **The first half is false in the tree: it reads YES.** The second half holds: R11's path refuses a yes that picks by number, writes nothing, says so, and leaves the question open. See plan section 2.1 |
| `max_items_per_post` is still 10 and nobody is held back | PASS: ten companies in scope, ten named |
| Test mode | the same M1 and M2 |

## 3. The messages, as posted in the check

```
New companies in the Master Pipeline — since Wed 7 Oct

1. Underdog AI (Conway Research)
2. Limbic AI — already on Outreach PoCs, missing some fields

Would you like me to look these up and suggest prospective PoCs we could contact?
```

```
Suggested PoCs

Underdog AI (Conway Research)

1. Sigil Wen — Founder & CEO
   Industry    AI Labs
   Email       sigil@underdog.ai
   Based       San Francisco, USA
   LinkedIn    linkedin.com/in/sigil
   Source      underdog.ai/team

2. Daniel Hong — Founding Team, Member of Technical Staff
   Industry    AI Labs
   Based       Seoul, South Korea
   LinkedIn    linkedin.com/in/unifiedh
   Paper       unifiedh.com
   Source      linkedin.com/company/underdog-ai/people

Limbic AI

1. Ross Harper — Co-founder & CEO
   Industry    Mental Health AI
   Based       London, UK
   LinkedIn    linkedin.com/in/rossgharper
   Source      limbic.ai/team

Some fields are missing because I could not find them on the web.

Shall I go ahead? This adds 4 rows for Underdog AI (Conway Research) and Limbic AI.
```

(The check's fixture has a third Underdog person, so its own M2 says 4 rows with three under Underdog; the block
above is trimmed to the agreed example.)

```
Missing fields

VoiceCare AI

Anil Keshav — Head of Clinical AI
   Email id    anil@voicecare.ai
   LI Url      linkedin.com/in/anilkeshav
   Source      voicecare.ai/about

Mara Ellis — Research Lead
   LI Url      linkedin.com/in/maraellis
   Source      voicecare.ai/research

Some fields are missing because I could not find them on the web.

Those columns are ones I'm not able to write to, so could you add them please?
```

```
Done — Underdog AI (Conway Research)
added to Outreach PoCs

Not added — Limbic AI
Skipped Ross Harper: already on Outreach PoCs.
```

```
No PoCs found — Nobody Labs

I'm sorry, I searched and could not find named people I would be confident suggesting.
Happy to try again with a starting point — a careers page or the LinkedIn company URL would be enough.
```

## 4. Not covered

- **The search itself.** `_find_people_data` and `_email_lookup` are stand-ins in the check. Whether a real search
  returns "Based" and a paper link in the two new optional fields is untested; when it does not, those lines are
  simply absent.
- **The real `append_row`** (band check, duplicate check, note placement, read-back) is not exercised by the new
  script; it is unchanged and has its own tests.
- **A message longer than one Discord message.** M2 is split between lines and the proposal is keyed to the last
  part; that path was written and not tested.
- **A day-of-week bug in my own NFT2-1069 self-test** was found and fixed on the way: `python drip.py` called
  `group()` without a day and so depended on the weekday it ran on (it failed on a Friday).

## 5. Environment variables

- **NEW:** `POC_MANDATORY_FIELDS=name,designation,li_url`, `POC_SUGGEST_MAX_PER_COMPANY=3`,
  `POC_FILL_MAX_ROWS_PER_COMPANY=5`.
- **CHANGED default:** none. **RETIRED:** none.
- **`EMAIL_WRITE_ALLOWED` is unchanged (false).** It does NOT need changing for this feature to work. It needs to be
  `true`, in both files, only if the team wants found emails WRITTEN: on a new row, or in a blank Email cell of an
  existing row.

## 6. The fix pass of 9 Oct (after the live run)

Eleven fixes from the live run in which Sigil Wen, already row 651, was offered as a new row. Run offline on the
final tree:

| Command | Result |
|---|---|
| `python verify_poc_crosscheck.py` (rewritten for the new behaviour) | ALL PASSED |
| `python -m pytest -q tests/` | 921 passed, 25 skipped, 0 failed |
| `python verify_websearch.py --offline` | ALL PASSED |
| `python verify_day_order.py` | ALL PASSED |
| `python -m poc_crosscheck`, `-m nextaction`, `-m websearch`, `-m wording`, `python drip.py` | ALL PASSED |
| `verify_poc_lookup.py`, `verify_layouts.py`, the rest of the verify sweep | not run (the first two are live-only) |

Sections 2 and 3 above describe the first build; the messages and the three-state M1 are now as
`python verify_poc_crosscheck.py --show` prints them. The search and the email lookup are stand-ins in the check, so
whether a real search honours the negative terms, returns a role rather than a fellowship, or finds anybody on the
second and third query angle is untested.
