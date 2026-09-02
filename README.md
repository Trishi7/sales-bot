# Sales & Marketing CoS bot

A Discord bot that acts as a sales & marketing chief of staff for the NFThing
team. It reads the sales channels, answers questions from what it can actually
see, and chases the deadlines people commit to in passing.

**It speaks unprompted exactly once a day.** Everything it has to chase, flag or
escalate goes out in [one daily digest](#the-one-daily-digest) at
`SALES_DIGEST_TIME`. Everything else it says is a reply to a person.

It is a **separate bot from the PM bot** — its own Discord application, its own
token, its own channels, its own database, its own venv, its own PM2 process.
Nothing is shared between the two.

---

## What it does

**Answers questions — only when tagged.** In **every** sales channel, the ask
channel (`SALES_ASK_CHANNEL_ID`) included, the bot answers only when it is
**@-mentioned** in the message text or when someone **replies directly to one of
its own messages**. A message that tags somebody else is never answered, even if
it also tags the bot. Questions go to a read-only tool-use loop that reads the
**GTM Playbook live** ("where are we with OpenAI", "which P1s have we never
contacted", "what do we pitch to a D2C brand"), reads the **researcher/buyer
mapping** ("who do we pitch at Anthropic for red-teaming", "give me T1 evals
champions"), searches the sales channels' history, reads the meeting notes, and
triangulates across all of them — citing the tab and the company, and saying
plainly when a cell is empty.

**Runs the daily cadence (phase 1).** Ten rules (a–j) from Vaishnavi's *Steps for
Sales Bot* doc run once a day against the GTM Playbook's **outreach tracker** —
the hidden "Outreach Updates" tab, and the only one with dates in it. Cold
follow-ups, connections with no intro behind them, positive replies with no next
step, meetings that left nothing behind. Rows whose rule inputs are blank become
*"update the tracker"* asks rather than false chases, and never-connected rows
older than the cold ceiling become **one counted line** rather than 756 nudges —
both on their own budgets. Everything becomes sections of the **existing** daily
digest, ranked urgent-first, with the urgent items **never truncated**. See
[The daily cadence](#the-daily-cadence-phase-1).

**Chases deadlines — and sets them.** When someone says "I'll send Acme the deck
tomorrow" in a sales channel, that becomes a *chase*. When a deal has **no**
deadline, the bot sets one itself, announces it with the rule it used, and invites
anyone to change it (see [Deadline authority](#deadline-authority)). Once a chase
is overdue it becomes a **line in the next daily digest** — never its own message
— for at most `COS_NUDGE_MAX_ATTEMPTS` digests, after which it moves to the
digest's ESCALATIONS section and the owner stops being chased. Stopping is
deliberate: a bot that keeps asking gets muted, and a muted bot enforces nothing.

**Keeps the sheet honest.** Three row-hygiene judgements — HOT, STALLED,
DEAD-DEAL — and the weekly funnel numbers. None of them is its own message
either: HOT leads the daily digest, STALLED and DEAD-DEAL are its HYGIENE
section, and the funnel numbers ride along on Fridays.

**Maps buyers to accounts.** The third sheet says *who* to pitch inside an
account, with the tier, the confidence, the ICP lane and the hook — and enforces
that sheet's own rules on every answer: departures are never recommended, stale
rows carry a re-verify caveat computed against today, and flagged non-buyers are
named as excluded rather than pitched. See
[The researcher/buyer mapping](#the-researcherbuyer-mapping-sheet-3-read-only).

**Cites the meeting.** Whenever meeting knowledge shapes a line — a hold, a
decision, a commitment — the line names its source: *"…on hold (Sales Bot
Discussion, 2 Sep)"*. Everywhere: digest items, cadence chases, the tracker
reminder, prep briefs, the to-do sheet, and answers. **A meeting-derived claim
with no meeting citation is a bug**, and there is no setting that turns it off.
See [Citing meetings](#citing-meetings).

**Keeps the team's to-do sheet.** One Google Sheet — *Membrane Sales To-Dos* —
that the bot creates, **shares with the team**, and appends action items to every
week out of the meeting notes, each row carrying the meeting it was committed in.
It never edits a row and never deletes one: Status and Notes belong to the
humans. See [The to-do sheet](#the-to-do-sheet).

**Checks outreach against the plan.** The strategy doc is no longer a stub. The
bot reads it read-only, reports how current it is, adopts any cadence it states
in place of the working-day defaults, and says weekly where outreach and the plan
disagree — in both directions. See [The strategy doc](#the-strategy-doc).

**Tells the truth about its blind spots.** Ask it what it can do and it names
every source it cannot currently read, and says what that means it can't answer.
That honesty is not a prompt preference — the same `sources.status_report()`
feeds the answer, the system prompt, and `state/summary.json`, so what it tells a
person and what it tells a supervisor process cannot drift apart.

**What it does not do.** No triage, no classification, no ticket creation, no
approval channel, nothing to approve. Its only output is a Discord message in a
sales channel. It contacts nobody outside Discord, and the only thing it ever
writes anywhere is one column on one tab of one spreadsheet. **And it does not
interrupt**: one unprompted message a day, and that is a property of the code,
not a setting — `_maybe_post_daily_digest` is the only proactive send path left
in `bot.py`.

---

## Hard guardrails

These are enforced **in code** (`guardrails.py`), not in prompt text. A rule that
only exists in a prompt is a suggestion.

| Rule | Where |
|---|---|
| **Never DMs anyone.** Not as a fallback, not for a failed post. | `guardrails.send()` refuses any non-guild destination |
| **Only @-mentions people on the team roster.** Everyone else is named in plain text. | `guardrails.mention_for()` is the only source of a mention token; `sanitize()` strips any the model invented |
| **Reads and posts only in `SALES_CHANNEL_IDS`.** | `guardrails.may_read()` gates every incoming message and every history scan; `send()` gates every post |
| **Answers only when tagged** — @-mentioned, or replied to. Never a message that tags someone else, never `@here`/`@everyone`, never another bot. | `SalesBot.should_respond()` in `bot.py` — the ONE gate, called by both `on_message` and the edit handler, logging `[gate] …` for every verdict |
| **Every action is audited** with a timestamp and a reason — including refusals. | `guardrails.send()` → `state.audit()` |

Code scoping is the **second** layer. The first is server-side: the bot's Discord
role must be denied **View Channel** on every non-sales channel. See
[DEPLOY.md](DEPLOY.md).

---

## Quick start

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then fill in the REQUIRED values
python main.py
```

Required: `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `SALES_CHANNEL_IDS`. For the GTM
sheet you also need `GOOGLE_SERVICE_ACCOUNT_JSON` **and both sheets shared with
the service account** (see [The GTM Playbook](#the-gtm-playbook)). Every variable
is documented and commented in `.env.example`.

Two Google setup steps are easy to miss, and both fail in ways that look like
something else:

1. **Enable the Google Drive API** on the service account's Cloud project. The
   to-do sheet cannot be created without it, and the strategy doc cannot be read.
   Google's answer to a project that has never enabled it is a bare
   `403 The caller does not have permission`, which reads as a key or scope
   problem and is neither — the bot translates it into the console URL that
   fixes it. See [The to-do sheet](#the-to-do-sheet).
2. **Share the strategy doc with the service account as Viewer**, and set
   `STRATEGY_DOC_ID`. See [The strategy doc](#the-strategy-doc).

Before a deploy, `python -m main --dry-run-digest 2026-09-07` prints that day's
whole digest and **sends nothing** — the fastest way to see what the team will
actually read.

The Discord application needs the **Message Content Intent** enabled (Developer
Portal → your app → Bot → Privileged Gateway Intents). Without it every message
arrives with empty content and the bot silently does nothing.

For production (PM2, process name `sales-bot`), see [DEPLOY.md](DEPLOY.md).

---

## When it answers

One rule, the same in **every** channel in `SALES_CHANNEL_IDS` — including
`SALES_ASK_CHANNEL_ID`. There is no channel with a no-mention mode.

| The message | Answered? |
|---|---|
| `@sales-bot where are we with OpenAI?` | **Yes** — the bot is @-mentioned in the text. |
| A reply to something the bot posted (an answer, the digest, a deadline announcement) | **Yes** — replying is how you talk to it without typing its name. |
| `where are we with OpenAI?` with nobody tagged, in any channel | No. |
| `@Vaishnavi can you check OpenAI?` | No — it tags someone else. |
| `@Vaishnavi @sales-bot thoughts?` | No — a message tagging somebody else is never answered, even when it also tags the bot. |
| A reply to **another person's** message | No — the parent has to be the bot's own message. |

The gate is **`SalesBot.should_respond()`** in `bot.py` — ONE function, the only
thing in the codebase that can authorise a reply to a human message. Both paths
that could produce one (`on_message` and `on_raw_message_edit`) call it and
nothing else, so a typo fixed in a tagged message re-fires, and an edited
untagged one still gets nothing. It returns `True` only when the author is a
human (never another bot, never the bot itself) **and** either the bot is
explicitly @-mentioned or the message replies to one of the bot's own messages.

**Every verdict is logged**, so you never have to guess why a message did or did
not get an answer:

```
[gate] responded msg=1234567890 reason=mentioned
[gate] responded msg=1234567891 reason=reply-to-bot
[gate] ignored   msg=1234567892 reason=ignored
```

If the bot ever answers something it shouldn't, that line names the message id
and the reason — grep the PM2 log for `[gate]` before anything else.

Three details worth knowing:

- **Only the message text is read for tags.** Discord silently adds a mention of
  the person you reply to; that is not you tagging them, so it never counts
  against a reply.
- **`SALES_ASK_CHANNEL_ID` is a posting home, not an answering rule.** It is
  where the daily digest and the deadline announcements land. Its behaviour on
  incoming messages is identical to every other sales channel. There is no
  `is_ask_channel()` branch in the answering path — the function no longer
  exists in the codebase.
- **`@here` / `@everyone` is never a bot mention.** `message.mention_everyone`
  is rejected before the mention test can even run, so a broadcast at the room
  is not a question for the bot.

Also unaffected: **passive reading**. The bot still watches every sales channel
for commitments to chase — it just does so silently, and the gate has no say in
it. What did **not** change: the daily digest, the ask-time deadline
announcement, and the reply-based deadline chasing — a reply to the bot's digest or to the
original promise still closes that chase, whether or not it also gets an answer.

---

## The policy file

`sales_policy.md` at the repo root is the bot's operating policy: its role, what
it enforces (strategy currency, outreach against the plan, deadlines), its tone,
and its hard limits.

**It is re-read on every question.** Edit the file, ask the next question, and
the new policy is already in force — no restart, no redeploy. `persona.py` caches
on the file's mtime and size, so an unchanged file costs one `stat()` per turn.

The role paragraph is explicitly marked as a placeholder for Sid to rewrite. The
rest of the file can stay as-is.

---

## Sources

| Source | Status | What it's for |
|---|---|---|
| `sales_spreadsheet` | **wired up** (Sheets API) | the GTM Playbook: outreach tracker, positioning matrix, prospect priority |
| `researcher_mapping` | **wired up** (Sheets API, **read-only**) | which researcher to pitch at each org, in which ICP lane, with what hook — and who must not be pitched |
| `sales_meeting_notes` | **wired up** (rclone sync + standup exclusion) | what was said, decided and committed to in the team's meetings — every synced note except the product standups. Everything derived from them carries a **citation** |
| `strategy_doc` | **wired up** (Drive API, **read-only**) | the current strategy, how current it is, and what outreach is checked against |
| `todo_sheet` | **wired up** (Drive + Sheets API, **append-only**) | *Membrane Sales To-Dos* — the shared action-item list |

Each source self-reports `connected` / `degraded` / `awaiting-access` / `error`
with a one-sentence detail a human can act on. **`degraded` means readable but
going stale** — the notes folder is fine, the sync that fills it is failing. The
bot may answer from a degraded source, but only while saying what is wrong with
it; it may not answer at all from `awaiting-access` or `error`.

`researcher_mapping` reports **degraded** rather than connected in one specific
case: the rows are readable but the legend or the departures list is not. That
matters because those two carry rules — the staleness threshold and the
do-not-pitch list — and a recommendation made without them looks identical to one
made with them. Degraded says "I can read the people, I cannot fully check them".

The playbook and the mapping are **two sources, not one**, because they answer
different questions (which *account* vs which *person*), they fail independently,
and only one of them is ever writable. Collapsing them would let an outage in one
be reported as health in the other.

`todo_sheet` is a source **because its failure mode is silent**. A sheet the
service account created but never shared is perfectly readable by the bot and
invisible to every human — from inside, it looks exactly like a healthy one. So
its status line reports **who can actually open it**, not merely whether the API
call worked, and it goes `degraded` when an address on `TEAM_SHARE_EMAILS` is
missing from the file's permissions.

`strategy_doc` goes **degraded rather than connected when the plan is stale**.
The answers built on it are still the best available, and every one of them has
to say the plan they are quoting hasn't been revised in a month.

---

### Meeting notes: the sync, and the standup exclusion

`notes.py` owns both halves of the pipeline.

**The sync.** `NOTES_SYNC_CMD` is a full rclone command. The bot runs it:

* once at **startup**, before it reports its source statuses,
* every **`NOTES_SYNC_MINUTES`** (default 30) on a background task of its own —
  separate from the chase sweeper, so a slow rclone can't delay a chase,
* **on demand** before answering a notes question. A question about a recent
  meeting ("what did we say today?") forces a pull; anything else only syncs if
  the last attempt is older than the interval, so asking twice doesn't run rclone
  twice.

`NOTES_DIR` is created if it doesn't exist. A failure — rclone missing, an
expired token, a timeout — **never crashes the bot and never spams the log**: it
is recorded, logged **once** at ERROR with the fix named, repeated identical
failures drop to DEBUG, and the source flips to `degraded` so answers say the
notes may be stale.

> **Windows: a PowerShell alias for `rclone` is invisible to a subprocess.** The
> sync runs through a shell subprocess, so an alias or function defined in your
> profile will fail with *"'rclone' is not recognized as an internal or external
> command"*. Put `rclone.exe` on `PATH`, or write the full path in the command:
> `NOTES_SYNC_CMD=C:\rclone\rclone.exe copy gdrive: ./notes --drive-shared-with-me --drive-export-formats txt`
> The same applies to a service account running under a different Windows user:
> it has its own `rclone.conf`.

**The filter is EXCLUDE-based.** The sync pulls *every* `Notes by Gemini` doc
shared with the sync account, and the bot loads **all of them except the
recurring product standups**. A doc is excluded when its **title** contains any
of `NOTES_EXCLUDE_TITLE_PATTERNS` (default `AM sync,PM sync`), matched
case-insensitively as a substring. Everything else — PM calls, customer calls,
ad-hoc meets — is loaded and readable.

Only the title is matched, never the body: a real meeting whose notes merely
*mention* "the PM sync" is still a real meeting. The title is the filename, or
the document's first line when the filename carries none.

> **Why exclude rather than include.** This used to be an include-list: a note
> counted only if its attendee list named a specific person or its title carried
> a sales keyword. On the live folder that admitted **zero of 72** synced docs —
> the invite list doesn't survive the Gemini export, and nobody titles a real
> call "sales sync". An include-list that matches nothing is indistinguishable
> from *"no meetings happened"*, which is the one failure this pipeline exists to
> prevent. The worst an exclude-list does is put a standup in context: visible,
> and harmless. On that same folder the default now loads 2 and excludes 70.

Each sync logs one line — `synced 72 docs, 2 loaded, 70 excluded (standups)` —
and the source status reports the same facts: when it last synced, how many docs
are on disk, how many went into context, and how many were held back.

**Nothing loaded is not "no notes".** With docs on disk but nothing loaded, the
source says *which* it is, because the two causes have different fixes: every doc
was a standup (widen or clear `NOTES_EXCLUDE_TITLE_PATTERNS`), or nothing that
came down carried a parseable date (so it isn't a meeting note at all). Set the
patterns empty and every synced note loads, standups included — legal, and logged
at startup so it isn't a surprise.

A file counts as a meeting note if its filename or first lines carry a date;
`.docx`, `.txt`, `.md` and `.html` are all readable, and the usual `Summary` /
`Decisions` / `Next steps` structure is parsed when present.

> rclone is used **only** for the meeting-notes docs. The spreadsheet is never
> exported, downloaded or synced — it is read live over the API.

---

## The GTM Playbook

The sales team's system of record. Read **live** through the Google Sheets API at
answer time (cached 60s to stay inside quota) — there is no sync interval and no
local copy.

### The two spreadsheets

| | Sheet | Role |
|---|---|---|
| `GTM_SHEET_ORIGINAL_ID` | *NFThing <> GTM Playbook* | **READ-ONLY** source of truth |
| `GTM_SHEET_COPY_ID` | *… — BOT COPY (sandbox)* | the bot's writable mirror, and the default write target |

### Auth, and the one setup step people miss

`GOOGLE_SERVICE_ACCOUNT_JSON` points at a service-account key file. Creating the
key is not enough — **both sheets must be shared with the service account's
address**: the original as *Viewer*, the sandbox copy as *Editor*.

Without that, every read is a `403` and the bot reports the spreadsheet as
awaiting-access. It does not crash, and it does not answer sheet questions from
guesswork; it logs the exact line to fix:

```
[bot] GTM original sheet NOT reachable: the service account cannot open the original sheet (permission denied)
[bot] ACTION REQUIRED: Share "NFThing <> GTM Playbook" with sales-bot@… as Viewer (open the sheet, click Share, paste the address, set Viewer, Send).
```

The key file is a secret: `.gitignore` covers the usual key filenames. If one is
ever committed, **revoke it in Google Cloud** — deleting the file is not enough.

### The tabs, identified by header signature

Tabs are recognised **by the columns they carry, never by their names** — names
in this playbook drift, signatures don't. Each kind is defined by a small
signature of headers only that tab has:

| Kind | Signature (all required) | Live tab, 1 Sep |
|---|---|---|
| `outreach_tracker` | `Last followed up date` + `Total follow-ups till date` | **"Outreach Updates"** (hidden, 886 rows) |
| `master_data` | `Response Status` + `Intro Sent` + `Meeting Done` | **"Master Data"** (587 rows) |
| `lead_pipeline` | `Lead Stage` + `Estimated Value (INR)` | **"Lead Master Sheet"** (hidden, 333 rows) |
| `funnel_pivot` | `Vertical / Stage` | **"Sales Funnel - March-June 2026"** (hidden) |
| `researcher_lines` | `Outreach Line - Researchers` + `Dates` | **"Master Pipeline"** (271 rows) |
| `positioning_matrix` | `Use Case` + Problem / Offering / ICP / Business Impact | **"Sales Outreach Matrix"** (hidden) |
| `prospect_priority` | `Company` + `Priority` (+ score / rationale) | Fortune 500 and AI-agent lists |

**The tracker is the cadence source, and it is the only tab with dates in it.**
The master tab is status-only: Connected / Intro Sent / Response Status / Meeting
Done / Assets Shared and a `Month` that is a month, not a date. It answers
aggregate questions, defines the weekly funnel and is cross-checked against the
tracker — it cannot drive a single date rule.

**Which real tab got which role is logged at startup**, so that question is one
log line rather than a guess:

```
[gtm.roles] outreach_tracker   TRACKER — the cadence source (the only tab with dates) -> 'Outreach Updates' (886 rows, HIDDEN)
[gtm.roles] master_data        MASTER — status only (aggregates, funnel, cross-check) -> 'Master Data' (587 rows)
[gtm.roles] lead_pipeline      PIPELINE — lead stage / estimated value                -> 'Lead Master Sheet' (333 rows, HIDDEN)
[gtm.roles] funnel_pivot       FUNNEL PIVOT — the funnel stage definition             -> 'Sales Funnel - March-June 2026' (38 rows, HIDDEN)
[gtm.roles] researcher_lines   RESEARCHER LINES — outreach lines for researchers      -> 'Master Pipeline' (271 rows)
```

**Hidden tabs are read** (`worksheets(exclude_hidden=False)`), and that is not
optional here: the cadence source itself is a hidden tab.

**Headers are discovered at parse time** and matched to roles by alias, because
the tabs keep evolving. Nothing is positional. Specific aliases beat loose ones
(*"Last followed up date"* wins over a bare *"date"*), a header maps to at most
one role, and any column that matches nothing is still carried as `_extra` — so a
question about a column this code has never heard of is still answerable.

**Spreadsheet errors are not data.** A `#REF!` / `#N/A` / `#VALUE!` left behind
by a broken formula is normalised to **empty** at parse time, and the raw cell is
remembered so the digest can report it once (see *sheet-health flags*). Reading
one as text made a date column unparseable and a status column read as
"responded".

When wording is genuinely ambiguous, override it:

```bash
GTM_COLUMN_MAP={"outreach_tracker":{"company":"Client Name","owner":"Owned By"}}
```

A one-line schema summary is logged per tab at startup:

```
[gtm] original tab 'Outreach Updates' kind=outreach_tracker rows=886 cols=18 mapped=[assets_shared, company, …] [HIDDEN TAB]
```

### Reads, and what happens when the API is down

Live at answer time, cached for `SHEET_CACHE_SECONDS` (60). One refresh of a
spreadsheet costs **3 API calls** regardless of how many tabs it has: all tabs
come back in a single `values.batchGet`. This matters because Sheets allows 60
reads per minute per user — the obvious one-request-per-tab version spent ~21
calls per spreadsheet, so two refreshes of both sheets inside a minute exhausted
the quota and pushed the bot onto stale cache for no reason.

On any API failure — quota, auth, a 5xx — the bot answers from the **last good
read** and the answer carries an explicit staleness note:

> (read from the sheet 3 hour(s) ago — I couldn't reach the Sheets API just now,
> so this may be out of date)

A sheet outage never takes the bot down, and never silently passes stale data off
as current.

### Writes — deliberately tiny

`SHEET_WRITE_TARGET` = `copy` (default) · `original` · `off`.

The entire write surface:

- **One column**, `Next Deadline (bot)`, appended at the **far right** of the
  tracker tab. Appending a header is the only structural change the bot ever
  makes, and only at the right-hand edge, so no existing column moves.
- **One cell at a time**, via `values.update` on a single-cell range. Never a
  full row, never a full sheet, never any other column.
- **A human-entered date always wins.** Cells the bot writes are marked
  `2026-08-26 (bot)`; anything without that marker is treated as human-entered,
  is **adopted** into the bot's own store, and is **never overwritten**.
- **The target row must still name the company we think it does.** Row numbers
  are discovered on the ORIGINAL but written to whatever `SHEET_WRITE_TARGET`
  names — sound only while the two sheets stay row-aligned, and nothing
  guarantees they do. Every write re-checks the company in the target row and is
  **refused**, logged and audited if it disagrees, rather than stamping a
  deadline onto the wrong company in a column nobody is watching.
- Every write — success or failure — is logged to `state/audit.jsonl` as
  `sheet_write`.

SQLite is authoritative; the sheet is a mirror. A deadline exists once it is in
the `deadlines` table, so a failed sheet write loses nothing.

#### Verifying the write path

```bash
python -m gtm_sheet            # access check + schema dump for both sheets
python -m gtm_sheet --write    # ... and a full write round-trip on the SANDBOX
```

`--write` writes one cell, reads it back and confirms it matches, so the whole
path — auth, the column, the cell address, the value — is proven end to end. It
**refuses to run unless `SHEET_WRITE_TARGET=copy`**: the check writes a real
cell, and the only sheet this bot may experiment on is the sandbox. The
read-back is retried a few times, because Sheets occasionally returns the
pre-write value immediately after an update and reporting that as a failed write
would be a lie about the one thing the check exists to establish.

---

## The researcher/buyer mapping sheet (#3, read-only)

`GTM_MAPPING_SHEET_ID` — *"membrane.social - Researcher Buyer Mapping"*.

The GTM Playbook says which **accounts** we are working. This sheet says which
**people** to approach inside them: the ICP lane, the tier, the evidence behind
the role claim, the hook to open with — and, just as often, that nobody there
should be approached at all.

### Read-only, enforced three ways

The bot **never** writes to this sheet. That is not a convention, it is three
independent locks, any one of which is sufficient:

1. **No write method.** `mapping_sheet.py` exposes no update, append, or
   ensure-column. There is nothing to call.
2. **A read-only token.** Its client is built with the `spreadsheets.readonly`
   scope — a *different, narrower* scope from the one `gtm_sheet.py` uses. Google
   itself refuses a write with that token, whatever the code asks for.
3. **Every write path refuses the id.** `config.is_read_only_sheet_id()` names
   this spreadsheet, and `gtm_sheet.py` checks it before touching a cell. Point
   `GTM_SHEET_COPY_ID` at this sheet by mistake and the write is refused with an
   explanation; `config.validate()` additionally forces `SHEET_WRITE_TARGET=off`
   and says so at ERROR.

Share it as **Viewer**. Editor is not needed and the startup remedy line asks for
Viewer precisely so nobody widens it out of habit.

### The legend is behaviour, not a glossary

The `Mapping _Legend` tab states the rules under which the sheet's data may be
quoted. They are **loaded and enforced**, not merely readable:

| Rule | How it is enforced |
|---|---|
| **ICP lanes** a = model evals/benchmarks, b = post-training/RLHF/preference data, c = red-teaming/safety, d = agent trajectories & personalization | read *from* the legend into `Legend.lanes`; `who_to_pitch(lane=…)` resolves a phrase like "red-teaming" against those descriptions, so re-wording the legend changes how questions resolve |
| **Tiers** T1 = verified champion, pitch-ready; T2 = strong fit, re-verify; T3 = door-opener/watch | parsed into `Legend.tiers`; every row carries its tier *and* the legend's definition of it |
| **Confidence is evidence quality and INDEPENDENT of Tier** | carried as two separate fields on every row, restated as a `you_must` rule on every tool result, and repeated in the policy and the answer prompt. The bot may never merge them into one score |
| **Staleness** — "re-verify any row older than ~6 weeks" | the threshold is **read from the legend** and measured against **today**, never hardcoded. Two clocks: the age of the research pass, and the age of the row's own *role verification*. Past either, the row gets an explicit `re-verify role before outreach` caveat that the prompt forbids trimming |
| **Departures** — check before any outreach | the `Edge Map`'s DEPARTURES row is parsed into a do-not-pitch list. A departed person is never recommended, and the answer names the move ("left Flipkart for Microsoft"). If the list can't be read, the answer must say the check *didn't run* |
| **Flags** — non-buyers and budget-gate failures | read out of the legend's own *Known gaps* bullets, so adding a fifth non-buyer there enforces it without a code change. Backed up by the `Org Coverage` account notes. Flagged orgs are reported as excluded, with the sheet's reason |
| **Watch-outs** | each row's caveat travels attached to its hook, and the prompt requires stating it in the same breath |

Staleness deserves one note on calibration. Only the **Role Verified Via** column
feeds the per-row clock. `Key Evidence` dates *papers*, not employment — measuring
role currency off publication dates put a caveat on nearly every row and made the
caveat meaningless. A year-only citation from the same year as the research pass
is treated as the pass date rather than as January, so a bare "2026" doesn't
manufacture staleness out of a guess. The newest cited evidence is still reported;
it just doesn't trigger.

### Tabs, discovered dynamically

Known tabs are the legend, `Researcher Mapping`, `Org Coverage` and `Edge Map`,
but they are matched by **what their headers contain**, so a rename doesn't blind
the reader. The workbook's other tabs (an academic-outreach list, a research-topic
list) match no signature and are logged once and ignored — `Tier` and `Confidence`
are mandatory for the researcher kind precisely so the academic list, which also
has a "Researcher Name" column, is never read as one. `GTM_MAPPING_COLUMN_MAP`
overrides the match when wording is genuinely ambiguous.

### The five tools

| Tool | Answers |
|---|---|
| `who_to_pitch` | "who do we pitch at &lt;org&gt;", "…for &lt;lane&gt;", "give me T1 evals champions", "pitch hook for &lt;person&gt;". Filters combine |
| `mapping_rules` | what a lane or tier *means*, how current the sheet is, the departures list, why an org is excluded |
| `mapping_coverage` | "which orgs have no mapped researchers" (`only_gaps`), plus one org's coverage and account note |
| `mapping_edges` | warm-intro paths — shared labs, investors, alumni networks. Routes, not targets; the departures row is deliberately excluded |
| `cross_check_outreach` | **the cross-source answer**: the tracker's PoC beside the mapping's suggestion, *and* a check of that PoC against the departures list |

Every researcher row is passed through `MAPPING.enrich()` before the model sees
it, so the departure check, the staleness verdict, the org flags and the row's
watch-outs arrive **attached to** the hook rather than in a separate paragraph the
model might not read. A tool that returned a bare hook would be a tool that let
the sheet's rules be skipped.

The cross-source shape is the one the team asked for:

> we're talking to &lt;PoC&gt; at &lt;org&gt; (tracker, 12 Aug); the mapping suggests
> &lt;researcher&gt; (T1, Confidence: High, lane a) — hook: &lt;why them&gt;, watch-out:
> &lt;watch-outs&gt;.

And `cross_check_outreach` produces the line nothing else can: **the tracker has
no idea the PoC it names has changed jobs.** The departures check catches it.

### Three empties, three answers

A question about an org that returns nothing has three distinct meanings, and the
tools keep them apart because collapsing them is how a decision gets reported as
an oversight:

- **no mapped researcher** — the org is in the coverage list, the note usually
  says why (too big, wrong data modality, watch-only);
- **not in the sheet at all** — the mapping exercise never covered them;
- **deliberately excluded** — competitor, channel partner, or budget-gate.

### Self-test

```bash
python -m mapping_sheet                      # access, schema, legend, departures
python -m mapping_sheet --org "OpenAI"       # everyone mapped there, rules applied
python -m mapping_sheet --lane red-teaming   # by ICP lane (letter or phrase)
python -m mapping_sheet --tier T1 --json     # by tier, as JSON
python -m mapping_sheet --gaps               # orgs with no mapped researchers
```

There is no write test, and that absence is the point: there is no write path to
verify.

---

## Deadline authority

Kushal-sanctioned. Asked about a deadline that doesn't exist, the bot **sets
one** rather than asking which date you'd like:

> No deadline was set for Emami — setting the follow-up to Wed 26 Aug 2026
> (outreach follow-up: 3 working day(s) after the last touch). @Kushal
> @Vaishnavi shout to change.

The announcement is the consent mechanism, not politeness — a default someone can
push back on beats an empty cell nobody owns. It is never skipped when a date was
actually set, and it is **never** repeated for a deadline that already existed.

**Where the date comes from**, in order:

1. The **strategy doc's cadence**, when that document is readable (it isn't yet).
2. The defaults, in **working days, IST**: `OUTREACH_FOLLOWUP_DAYS` (3),
   `REPLY_CHASE_DAYS` (2), `MEETING_PREP_DAYS` (1).

Working days and IST are both deliberate: a follow-up due "in 3 days" set on a
Thursday must not land on Sunday, and a date computed in UTC would drift a day for
anything set after 18:30 local. The rule counts from the **row's own dates** — a
follow-up is due N days after the last touch, not N days after someone asked.

**No deadline is ever set in the past.** Anchoring to the last touch means the
rows that most need a deadline — the ones nobody has touched in weeks — are
exactly the ones whose computed date has already gone by. When the rule lands
before today the date is pulled to the next working day and the announcement
says why:

> ... (outreach follow-up: 3 working day(s) after the last touch; that fell 25
> working day(s) ago, so it is due now)

**This announcement is the one immediate proactive-looking message the bot still
sends, and it is the documented exception to the daily-digest rule.** It is not
an interruption: someone just asked when the follow-up for X is, and this is the
answer, carrying the "shout to change" invitation that makes the date
consent-based rather than imposed. Holding it until the next morning's digest
would answer a question a day late, so it goes out immediately.

The bot no longer sets deadlines **unprompted**. It used to do that for
dead-deal rows during the sweep, and announcing each one was exactly the kind of
scattered proactive message the digest replaced. Those rows are named in the
digest's HYGIENE section instead, and a person — or the bot, when asked — sets
the date.

**Chasing** happens entirely inside the daily digest: due today or on the next
working day appears under DEADLINES, past due under OVERDUE with its
working-days age, and once `COS_NUDGE_MAX_ATTEMPTS` appearances are spent the
item moves to ESCALATIONS addressed to `ESCALATE_TO_ID` and the owner stops
being chased. An "attempt" is an appearance in a digest, so the digest's
once-a-day guarantee is itself the rate limit.

---

## Row hygiene flags

Three judgements read off the tracker. **None of them posts a message of its
own**: HOT is the first section of [the daily digest](#the-one-daily-digest) and
STALLED / DEAD-DEAL are its HYGIENE section, so each is surfaced once a day by
construction rather than by a per-row rate limit.

| Flag | Fires when | Why it matters |
|---|---|---|
| **HOT** | `Response?` = yes, and the last follow-up is missing or predates the reply | Speed to lead. A prospect who answered and heard nothing back is a deal lost to silence. Same-day. |
| **STALLED** | open, no response, nothing scheduled, last touch older than `STALLED_AFTER_DAYS` (5 working days) | Silence kills deals. |
| **DEAD-DEAL** | open, empty Next Steps **and** no future date anywhere on the row | No next step = dead deal. Asks for the next action or a park **with a Reason**. |

Each flag states the cells it read, so the team can check it rather than argue
with it. A row with a booked future meeting is never flagged stalled or dead — it
is waiting, not drifting. A row with a `Reason` is parked, and is never flagged at
all. Each row gets at most one flag; when a row qualifies for several, the most
actionable framing wins (hot → dead → stalled).

Only **engaged** rows can stall or go dead: a row where something came back or
something real went out (an intro, a reply, a meeting, assets shared, or at least
one recorded follow-up). A cold name emailed once is an *unworked lead*, not a
dead deal. Without that gate "no next step = dead deal" fired on 756 of the live
sheet's 886 rows — a wall of noise that would get the bot muted on day one.

Deduplication is by **sheet row**, not by company. The tracker holds one row per
PoC, so a single company legitimately owns dozens of rows; keying the dedup by
company meant the first flagged OpenAI row silenced every other OpenAI row in
every category, which on the live sheet hid all 70 stalled rows behind rows
already claimed by the hot and dead-deal finders. Volume is bounded by
`SALES_DIGEST_MAX_PER_SECTION` instead, which — unlike the old per-sweep cap —
says how many rows it left out rather than silently deferring them.

---

## Weekly funnel numbers

**The funnel definition is the playbook's own**, taken from the *"Sales Funnel"*
pivot tab (signature: `Vertical / Stage`):

> Contacted → Connected → Intro Sent → Positive (P/Y) → Meeting Done → Assets Shared

```
**WEEKLY FUNNEL**
FUNNEL (587 rows on the master tab) — Contacted 587 -> Connected 185 -> Intro Sent 145 -> Positive (P/Y) 18 -> Meeting Done 9 -> Assets Shared 59
CONVERSION — connected 32% · intro sent 78% · positive (p/y) 12% · meeting done 50%
Window 2026-08-14 → 2026-08-21
LEADING — outreach sent 12 · replies 3 (25.0%) · meetings booked 2 · follow-ups done 8 vs 5 due
LAGGING — pilots 1 · paid 0 · repeats 0
HYGIENE — 2 row(s) with no named PoC · 3 with no next step
```

**The stage counts are recomputed from the master tab, not read out of the
pivot.** A pivot is a snapshot with a date range baked into its title (*"Sales
Funnel - March-June 2026"*), and reporting last quarter's cached totals as this
week's funnel is exactly the kind of quietly-wrong number a digest must not
carry. The master tab is what the pivot is a pivot *of*, so recomputing from it
reproduces the pivot when the pivot is current and is right when it isn't.

**Assets Shared is deliberately not nested under Meeting Done**, which is why it
gets no conversion percentage: the live sheet has 59 rows with assets shared
against 9 meetings done, because assets go out without a meeting all the time. A
funnel drawn as a strict nesting would report that as an error rather than as
what the team does.

The leading/lagging block below it reads the **tracker's dates** and answers a
different question — what happened this week, rather than where the pipeline
stands. Leading first, because those are the numbers still changeable this week;
lagging is reported, not acted on. Rows whose dates can't be read are counted
under HYGIENE rather than dropped, so a shrinking denominator stays visible
instead of quietly flattering the numbers.

These used to be their own scheduled Friday post. They aren't any more — a second
unprompted message a week is still a second unprompted message. They ride along
as a **section of the daily digest** on `WEEKLY_DIGEST_WEEKDAY` (Friday by
default), which means they appear only if that day's digest has something in it.
`WEEKLY_DIGEST_HOUR_IST` is retired: their time is `SALES_DIGEST_TIME`.

---

## The daily cadence (phase 1)

Ten rules (**a–j**) from Vaishnavi's **"Steps for Sales Bot"** doc, plus the
priority and cap decisions from the **27 Aug alignment meeting**. They run once a
day against the **outreach tracker** and feed the **existing** daily digest.

> **No rule in `cadence.py` can make the bot speak.** There is no
> `guardrails.send` in that file and there must never be one. Every finding is
> handed to `_maybe_post_daily_digest`, which is still the only proactive send
> path in the codebase. The mention-only gate is untouched — the cadence changes
> what the one daily message *says*, not how often the bot talks.

### It runs on the tracker, not the master tab

The cadence source is the tab whose headers carry **`Last followed up date`** and
**`Total follow-ups till date`** — live, the hidden **"Outreach Updates"** tab.
It is the only tab in the spreadsheet with dates in it.

The **master tab** ("Master Data": `Response Status` + `Intro Sent` + `Meeting
Done`) is **status only**. It has no last-followed-up date, no follow-up count,
no meeting date and no next steps, and its `Month` is a month, not a date.
Pointed at it, rules **a, c, h, i and j** read a blank and silently never fired —
the cadence looked calm because it was blind. The master tab now does the three
jobs it can actually do:

- **aggregate and pivot answers** — 587 rows of clean status;
- **the weekly funnel definition** (below);
- **a nightly consistency cross-check against the tracker**. Where the two
  disagree, the bot reports **both values** as an UPDATE-TRACKER line and
  **never infers a winner** — picking one silently would be a bot rewriting a
  status nobody asked it to touch.

**Statuses conveyed by cell colour are invisible to this bot.** It reads cell
*values* only — the Sheets values API does not return fills. That failure is
silent by nature, so at startup any mapped column that is empty on nearly every
row is named in a warning:

```
WARNING [gtm] tab 'Outreach Updates': the meeting_date column ('Meeting Date') is
filled on 9 of 886 rows. It MAY BE COLOR-CODED — this bot reads cell VALUES only
and cannot see fills, so every rule that reads meeting_date will see a blank.
```

### Vocabulary normalisation

The two tabs say the same things in different words, so everything goes through
one normaliser before a rule sees it:

| | Values | Reads as |
|---|---|---|
| **Response** | `Y`, `P`, `Did Respond`, `P - Positive/In Progress` | POSITIVE |
| | `N`, `N - Rejected`, `No` | REJECTED |
| | blank, `No Response`, `Awaited` | NONE |
| | anything else | *replied, polarity unclear* — never chased as silent, and **listed once** so the sheet can be standardised |
| **Connected** | tracker `Y` / blank · master `Yes` / `No` | yes / no |
| **Dates** | `9-Jun-2026`, `17-Mar-2026`, `22/05/2026`, `24-06-2026` | parsed |
| | two dates in one cell (a meeting that moved) | the **latest** wins |
| | `#REF!`, `#N/A`, `#VALUE!` | **empty**, and flagged |
| **Follow-up count** | blank | **0 *and* "not recorded"** — see below |

### Missing data is an ask, not a chase

The tracker is sparse: the follow-up count is blank on 800 of 886 rows, next
steps on 871. So **a rule fires as a CHASE only when every cell it depends on is
actually filled.** When a required cell is blank the row becomes an
**UPDATE-TRACKER fill-in ask** instead:

```
• OpenAI · Mark Chen (Chief Research Officer) — no last-followed-up date. Update the tracker.
```

Rules **(b)** and **(f)** are the exceptions — an empty cell *is* the signal
there, which is the whole point of them.

Without this split the digest would read a blank counter as "0 follow-ups" and
chase a row nobody had touched, using the sheet's own gaps as evidence against
the team. A gap is only asked about on a row that is **actually in play**: the
missing last-followed-up date only when the row shows engagement, the missing
count only when there is evidence of following up. "We never started" is rule
(c)'s business, not a bookkeeping gap.

### Re-pointing at a new sheet is an env change plus a restart

This was promised in the meeting, so it is a property of the code. Nothing about
the sheet's identity or its column names is compiled in:

| Setting | What it moves |
|---|---|
| `GTM_SHEET_ORIGINAL_ID` | which spreadsheet |
| `GTM_COLUMN_MAP` | which header means which rule field |
| `SALES_DEFAULT_OWNER_ID` | who every cadence line is addressed to |
| `GTM_MASTER_TAB_TITLES` | an optional *name hint* for the master tab — a hint, never an authority |
| `FOLLOWUP_STALE_DAYS`, `CONNECT_REMINDER_DAYS`, `CONNECT_REMINDER_MAX_DAYS`, `ALT_CHANNEL_AT`, `UNRESPONSIVE_AT`, `NEXTSTEP_STALL_DAYS`, `MEETING_PREP_DAYS`, `URGENT_MAX`, `DIGEST_MAX_ITEMS`, `UPDATE_TRACKER_MAX` | every threshold |

Change those, restart, done. No migration and no code edit. `GTM_COLUMN_MAP`
accepts every rule field as a role — for `outreach_tracker`: `company`,
`industry`, `poc`, `poc_designation`, `poc_vertical`, `first_contacted`,
`use_case`, `connected`, `intro_date`, `last_followed_up`, `followups_count`,
`response`, `reason`, `meeting_date`, `assets_shared`, `next_steps`,
`other_updates`, `owner`, `status`.

### Who a line is addressed to

**The tracker has no owner column.** Every row is worked by the same person
today, so a cadence line resolves its owner in two steps: the row's own owner
cell *if a column ever appears* (or `GTM_COLUMN_MAP` names one), then
`SALES_DEFAULT_OWNER_ID`. A ping still needs roster membership — an id that isn't
in `TEAM_ROSTER_IDS` gets named in plain text instead, which is the roster gate
failing closed.

### The ten rules

**Every `n` is a placeholder.** Vaishnavi's numbers were left unset in the doc;
the defaults below are guesses she will tune once the digest has been read for a
week — which is a restart, not a deploy.

| | Rule | Fires when | Needs filled | Threshold | Priority | Section |
|---|---|---|---|---|---|---|
| **a** | stale follow-up | Last followed-up date older than *n* **and** no response | the date | `FOLLOWUP_STALE_DAYS=5` | waiting | FOLLOW-UPS |
| **b** | intro pending | Connected = Y but membrane Intro Date blank | — *(blank is the signal)* | — | intro | INTROS |
| **c** | start interacting | First Contacted set, never Connected, and **inside the window** | the date | `CONNECT_REMINDER_DAYS=7` … `CONNECT_REMINDER_MAX_DAYS=30` | waiting | FOLLOW-UPS |
| | *cold summary* | the never-connected rows **past** the ceiling — one line for all of them, **never capped** | the date | `CONNECT_REMINDER_MAX_DAYS=30` | — | FOLLOW-UPS |
| **d** | another channel | Follow-ups ≥ *n*, no response → email / WhatsApp / call | the count | `ALT_CHANNEL_AT=4` | waiting | FOLLOW-UPS |
| **e** | mark unresponsive | Follow-ups ≥ *n*, no response → **ask the owner** to mark the PoC unresponsive | the count | `UNRESPONSIVE_AT=7` | waiting | UPDATE TRACKER |
| **f** | lock a meeting | Response positive, Next Steps blank, no meeting on the row | — *(blank is the signal)* | — | **urgent** | MEETINGS |
| **g** | another PoC | a **rejected** company with nothing else live there — **once per company** | — | — | suggestion | FOLLOW-UPS |
| **h** | meeting soon | Meeting Date within *n* days → earns the prep brief | the date | `MEETING_PREP_DAYS=4` | **urgent** | MEETINGS |
| **i** | post-meeting gap | Meeting Date past **and** Assets Shared **or** Next Steps blank | the date | — | **urgent** | ASSETS |
| **j** | next-step stall | Next Steps present but unchanged for *n* days | the cell | `NEXTSTEP_STALL_DAYS=10` | waiting | FOLLOW-UPS |

Four details that are load-bearing:

- **Rule (e) asks. It never writes.** The bot's only writable cell anywhere
  remains its own `BOT_DEADLINE_COLUMN`. Marking a person unresponsive is a
  judgement with consequences for the relationship, and it stays with the human
  who owns the row. The digest line says so out loud: *"Please mark Rob N
  'unresponsive' in the tracker (I don't write that cell)."*
- **Rules (f) and (g) were in conflict, and are reconciled.** Her doc says
  *"response = N / no response → suggest another PoC"*; the 27 Aug meeting
  excluded rejected leads from the cadence entirely. Both survive, split by what
  each was for: a rejected row is **never chased**, and instead the **company**
  gets **one** suggestion line naming an alternative PoC — skipped when somebody
  else at that company is still in play. "No response" with a high follow-up
  count is not a rejection at all; (d) and (e) already own that row.
- **Rule (g) pulls candidates from the researcher/buyer mapping** when the org is
  mapped — **with its usual caveats** folded into the line (staleness verdict,
  departure check, org flags). A mapped name quoted without them is exactly the
  mistake that sheet's legend warns about.
- **Rule (j) needs history the sheet doesn't have.** A spreadsheet cell does not
  know when it last changed, so the bot keeps its own: `nextstep_state` in SQLite
  holds a hash of each row's Next Steps text and the date that text first
  appeared. A fresh database earns these findings over the following days rather
  than firing a wall of them on day one, and editing the cell resets the clock.

### Exclusions and priority

**Rejected rows are excluded from every rule and every digest section** — dropped
before any rule is evaluated, not filtered out afterwards. They are revisited
offline by humans. A row counts as rejected when its **Response** says so (`N`,
`N - Rejected`, `No`) or when any of its cells contains one of
`CADENCE_REJECTED_MARKERS` (`rejected`, `not interested`, `closed lost`, …). The
one thing such a row still produces is rule (g)'s single per-company suggestion.

| Bucket | What's in it |
|---|---|
| **URGENT** | a positive response awaiting our action (f) · meetings within `MEETING_PREP_DAYS` (h) · post-meeting gaps (i) — **never truncated**, own budget |
| **WAITING** | a, c, d, e, j |
| **INTRO** | b — connected with no intro behind it |
| **SUGGESTION** | g — a "no" is real work, but it must never push a booked meeting off the digest |

### The cold ceiling: why rule (c) needed a maximum, not a bigger minimum

Rule (c) matched **756 of the live sheet's 886 rows**. Most of the tracker is
March–July outreach that never connected, and *"first contacted 158d ago and
still not connected — start interacting"* is not a task, it is an accusation
about last quarter.

**Raising `CONNECT_REMINDER_DAYS` is the wrong lever.** Those rows are older
than any threshold you could set, so a higher floor still lets every one of them
through. A **ceiling** is the only thing that separates the two populations:

| | Window | What it means | What the digest does |
|---|---|---|---|
| chase | `CONNECT_REMINDER_DAYS` … `CONNECT_REMINDER_MAX_DAYS` (7–30) | contacted recently, hasn't connected yet | one line per row: *"start interacting"* |
| **cold** | older than `CONNECT_REMINDER_MAX_DAYS` (30) | contacted long ago, never got anywhere | **one line for all of them** |

```
• 756 cold contacts across 209 companies (never connected, first contacted
  Mar-Jul 2026) — ask 'cold list' to see them.
```

**That line is not capped.** It sits outside `URGENT_MAX`, `DIGEST_MAX_ITEMS` and
`UPDATE_TRACKER_MAX` alike, because a count of suppressed rows that could itself
be suppressed would be worse than not suppressing anything — it would just be a
bot quietly hiding 756 rows. Asking **`cold list`** returns the whole thing,
uncapped and grouped by company, oldest first, since the decision it supports is
per-company (*"do we go back at OLX at all?"*) rather than per person.

**Only rule (c) is suppressed, not the row.** A March row that is still being
followed up every week is live work, and its (a), (d) and (e) lines still fire.
What's wrong on it is specifically the nudge to *start*, which is five months out
of date. Cold rows are also **not rejected** — nobody said no, they just never
answered — and the `cold_list` tool says so, because the two get confused.

`CONNECT_REMINDER_MAX_DAYS=0` turns the ceiling off and goes back to chasing all
756 individually.

### Three budgets, and the urgent items are never truncated

One number could not serve them, so there are three:

| Budget | Default | Covers | Truncates? |
|---|---|---|---|
| `URGENT_MAX` | **25** | (f) positive replies awaiting a next step, (h) meetings inside `MEETING_PREP_DAYS`, (i) post-meeting gaps | **No** — a hard ceiling against a broken sheet, not a target |
| `DIGEST_MAX_ITEMS` | **15** | everything else in the cadence — a, b, c, d, e, g, j | yes, counted in the closing line |
| `UPDATE_TRACKER_MAX` | **5** | the fill-in asks, the master/tracker disagreements, the sheet-health flags | yes, counted in the closing line |

**The urgent items are what Vaishnavi prioritised, so truncating them defeats the
digest.** Sharing one fifteen-item budget with the rest of the cadence, the live
sheet's sixteen urgent rows filled the entire digest on day one — and one row
later would have started cutting the positives themselves. `URGENT_MAX` exists
only so that a broken sheet (a column that suddenly reads as positive on every
row) cannot produce a thousand-line message; if it is ever actually hit the bot
logs a warning saying so, because that is a bug to look at rather than a number
to raise.

The **cold summary** is outside all three, as above.

### The cap is honest

What doesn't fit is counted in one closing line:

```
_218 more held — ask 'full cadence list' for everything._
```

Asking that returns the **uncapped** list — cadence items *and* tracker asks,
ranked, with every item's rule letter and priority — the `cadence_list` tool,
which obeys the same mention-only gate as every other answer. `cold list` does
the same for the cold cohort. A silently truncated list would read as "there were
only fifteen things", and that is a lie the digest cannot afford to tell.

### Sheet-health flags, deduped until fixed

Three findings about the spreadsheet itself, one line each, in the UPDATE TRACKER
section:

1. **Broken formulas.** Every `#REF!` / `#N/A` / `#VALUE!` the bot read as empty,
   named by tab and column. Live on 1 Sep: one cell, `'Master Pipeline'` →
   `'Outreach Line - Researchers'`.
2. **Misaligned master rows** — a cell holding a value from the wrong column's
   vocabulary (`Mar-2026` in Connected, `No Response` in Meeting Done).
3. **Response values outside the known set**, listed once so the sheet can be
   standardised.

Each carries a **signature** of what was found, stored in SQLite
(`quality_flags`). A flag whose signature hasn't changed since it was last
reported is **not repeated** — a daily reminder about a `#REF!` everybody already
knows about is exactly the drip that gets a digest muted. Fix half of it and the
signature changes, so the remaining half is reported again. The recording happens
**after** the digest actually posts, so a refused send can't silence a flag for
good. `full cadence list` ignores the dedup entirely: somebody asking what is
wrong with the sheet wants the whole answer.

### Meeting-prep briefs

A meeting inside `MEETING_PREP_DAYS` earns **one** brief, attached to that day's
digest and **deduped in SQLite** (`prep_briefs`) so it is written once per meeting
rather than every day until the meeting happens. Moving the meeting to a new date
legitimately earns a fresh brief.

It assembles — and asserts nothing beyond — these four sources:

| Source | What it contributes |
|---|---|
| Outreach tracker | company, PoC, designation, vertical, industry, the full history, assets shared, next steps |
| Researcher Buyer Mapping | the PoC's mapped row: role, evidence, pitch hook, watch-outs — **with its caveats**, via `MAPPING.enrich()` |
| Positioning matrix | the matching use case, problem statement, offering and business impact |
| Meeting notes | anything on file about the same company, **each line carrying its meeting citation**, and an opening `⚠ ON HOLD — …` line when a meeting parked the account |

**Online research is not included, and the brief says so.** This bot has no web
access, so every brief carries:

```
**External / online research — pending web access decision**
  · Not included. This bot has no web access, so nothing here comes from outside
    the sheets and the meeting notes above. Recent news, funding, headcount and
    product launches have NOT been checked — look them up yourself before the call.
```

That section is deliberate rather than an omission: a brief that quietly left
external research out would read as *"there was nothing to find"*. Nothing
external is inferred, guessed or filled in from the model's own knowledge, and a
blank cell renders as **"not recorded"** — never as a confident sentence. The
person reading a brief is about to repeat it out loud in a meeting.

### Seeing it before you trust it

At every startup the bot runs the cadence and **logs which rows each rule fires
on today**, with the cap applied and the reason in terms of the cells it read. It
sends nothing.

It opens with **which real tab matched which role**, because "the cadence found
no rows" is almost always "the tracker was read as something else today":

```
[gtm.roles] outreach_tracker   TRACKER — the cadence source (the only tab with dates) -> 'Outreach Updates'
[gtm.roles] master_data        MASTER — status only (aggregates, funnel, cross-check) -> 'Master Data'

[cadence.dryrun] (a) stale_followup          65 item(s)
[cadence.dryrun]       · Acme Labs / Priya R [row 2] [waiting]
[cadence.dryrun]         why: Last followed up date is '2026-08-10'; threshold is 5d.
[cadence.dryrun] (cold) cold_summary            1 item(s)
[cadence.dryrun]         why: Past the 30d cold ceiling, so rule (c) does not chase
[cadence.dryrun]              them individually; the oldest was first contacted 166d ago.
```

The same report runs standalone, against the live sheet or against built-in
fixture rows:

```bash
python cadence.py            # the real tracker tab; read-only, sends nothing
python cadence.py --demo     # fixture rows: no sheet, no network, no database
```

`--demo` is a real test of the rules rather than of the network: it asserts that
every rule fires on its row, that a rejected row produces **no chase** (only its
one per-company suggestion), that a blank follow-up count becomes an ask rather
than a chase, that a row past the cold ceiling produces **no individual item** but
**does** produce the uncapped summary line, that no urgent item was truncated,
and that the cross-check and sheet-health flags fire.

Everything also lands in `audit.jsonl` as usual — a `cadence_dry_run` record at
startup (carrying the tab-role map), a `meeting_prep_brief` record per brief
written, and a `sheet_quality_flag` record per sheet-health flag reported.

---

## The one daily digest

**Every proactive thing this bot has to say goes out once a day, in one message,
at `SALES_DIGEST_TIME` (default `10:00`, wall-clock IST), in the sales channel.**

This is not a preference expressed in a prompt and it is not a rate limit that
could be relaxed. The individual deadline-reminder, deadline-chase, promise-nudge,
give-up-flag, row-flag and weekly-funnel send paths were **deleted** from
`bot.py`. What each of them used to say is now a *line* in one message, and
`_maybe_post_daily_digest` is the only proactive send left in the file. If you
want the bot to tell the team something new, add a section in `digest.py` — do
not add a `guardrails.send`.

The reason is not tidiness. A bot that drips six kinds of message through the day
gets muted, and a muted bot enforces nothing — which makes every rule in this
README decorative.

### What's in it, hot first

The five cadence sections are in **Vaishnavi's daily-cadence order** — *"Follow-ups
for this week · Intros to be done this week · Meetings for this week · Update
tracker · Assets to be shared this week"*.

```
**Daily sales digest — Tue 25 Aug 2026**

**HOT — they replied, nothing has gone back (1)**
• HOT — OpenAI · Sam Altman, CTO: they replied on Mon 18 Aug and the last follow-up
  (Fri 15 Aug) predates it. (3rd day)

**FOLLOW-UPS for this week (4)**
@Vaishnavi
• 756 cold contacts across 209 companies (never connected, first contacted
  Mar-Jul 2026) — ask 'cold list' to see them.
• Cinder · Dev M — first contacted 27d ago and still not connected. Start interacting.
• Acme Labs · Priya R — last followed up 18d ago, still no response. Follow up or park it.
• Gantry — Ivan S said no. Nobody else there is in play; try a different PoC.
  Mapped alternatives: Dr Ada Vance (mapped 5 wks ago).

**INTROS to be done this week (1)**
@Vaishnavi
• Borealis · Sam K — connected, but no membrane intro date recorded. Intro pending.

**MEETINGS for this week (2)**
@Vaishnavi
• Fathom · Lea W — responded (P) with no next step recorded. Lock a meeting.
• Ionic · Tara B — meeting in 2d (Sun 30 Aug 2026). Prep it.

**UPDATE TRACKER (4)**
@Vaishnavi
• Everest · Rob N — 8 follow-ups, no response. Please mark Rob N "unresponsive"
  in the tracker (I don't write that cell).
• Tab 'Master Pipeline' has 1 cell(s) holding a spreadsheet error (#REF!) in
  'Outreach Line - Researchers'. I read those as EMPTY — a broken formula is not
  a value.
• Anthropic · Sandeep Jha — master and tracker disagree: Intro Sent / membrane
  Intro Date: master says 'No', tracker says yes. Please fix whichever is wrong
  (I don't guess which).
• OpenAI · Mark Chen — no last-followed-up date. Update the tracker.

**ASSETS to be shared this week (1)**
@Vaishnavi
• Halcyon · Mei L — met 7d ago, no next steps. Send what you promised and write
  the next step.

_218 more held — ask 'full cadence list' for everything._

**DEADLINES — due today or tomorrow (2)**
• @Vaishnavi — Emami — the follow-up is due today (Tue 25 Aug 2026).
• @Kushal — Nykaa — the reply chase is due tomorrow (Wed 26 Aug 2026).

**OVERDUE (3)**
• @Vaishnavi — OpenAI follow-up overdue 2 wd (3rd day); Emami deck due today
• @team — Nykaa reply chase overdue 4 wd (2nd day)

**ESCALATIONS — asked enough, needs a decision (1)**
@Kushal
• Zomato — the meeting prep was due Fri 21 Aug (overdue 2 wd) and I've asked
  2 time(s) with nothing back. (4th day)

**WEEKLY FUNNEL**
Window 2026-08-18 → 2026-08-25
LEADING — outreach sent 12 · replies 3 (25.0%) · meetings booked 2 · follow-ups done 8 vs 5 due
LAGGING — pilots 1 · paid 0 · repeats 0

**HYGIENE — stalled and dead-deal rows (6)**
• STALLED — Acme · Priya, VP Eng: no response and the last touch (Last followed up
  date: Mon 11 Aug) was 9 working days ago.
• …and 15 more hygiene item(s) — ask me for the full list.
```

| Section | What it holds |
|---|---|
| **HOT** | Inbound replied, no follow-up from us. First, because a prospect who answered and heard nothing back is the most expensive failure in the sheet. |
| **FOLLOW-UPS this week** | Cadence rules a, c, d, g — cold follow-ups, never-connected rows, rows worth another channel, companies where the PoC said no. |
| **INTROS to be done** | Cadence rule b — connected, no membrane intro date. |
| **MEETINGS this week** | Cadence rule f and meetings inside `MEETING_PREP_DAYS`. |
| **ASSETS to be shared** | Cadence rule h — a meeting happened and nothing followed it. |
| **UPDATE-TRACKER reminders** | Cadence rules e and i — gaps the row's owner should fill, including the ask to mark a PoC unresponsive. |
| **DEADLINES** | Due today or on the next working day, addressed to the owner. |
| **OVERDUE** | Chases and deadlines past due, **grouped per owner with one @mention each**, every item carrying its working-days-overdue age. |
| **ESCALATIONS** | Past `COS_NUDGE_MAX_ATTEMPTS` → addressed to `ESCALATE_TO_ID` (Kushal), at the bottom, because it is the only part written for one person. |
| **HYGIENE** | Stalled and dead-deal rows — the flags that used to post separately. |
| **MEETING PREP** | One brief per meeting inside `MEETING_PREP_DAYS`, written once. Reference material, not a task — which is why it sits last and does not count towards "is there anything to post today". |

**One @mention per person.** Someone with four overdue items gets one line and
one ping, not four. That grouping is the anti-nag rule in code
(`digest.group_by_owner`); items nobody owns fall into a single trailing line
addressed to `DEADLINE_NOTIFY_IDS`.

### Once a day, across restarts

The date of the last digest is persisted in SQLite (`meta.sales_digest_date`),
written **after** the message posts. It is a date, not a timer, so a redeploy at
10:05 reads it back and stays quiet. If the marker can't be read at all the bot
does not post: a duplicate digest is worse than a missed one.

The digest can only go out on a sweeper tick, so it posts at the first tick at or
after `SALES_DIGEST_TIME` (keep `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` well under
an hour). One consequence worth knowing: if a day would have been empty at 10:00
and a hot row turns up at 14:00, the digest goes out at 14:00. That is still one
digest, and holding a replied-to prospect for twenty hours to protect a schedule
would be the wrong trade.

### Carry-forward, and why there is no "resolved" line

An unresolved item **reappears in the next digest wearing its age** — `(3rd day)`.
An item resolved during the day (a date filled in, a reply sent, a promise kept)
simply isn't collected any more and drops out silently. There is no "resolved"
message: announcing resolutions would double the volume of the thing the digest
exists to reduce, and the team already knows what they fixed.

The age lives in the `digest_items` table, not in memory, so a redeploy doesn't
reset every item to day one — an item's age is the most useful thing on its line,
and one that silently restarts after every push is worse than no age at all.
`db.note_digest_item` is idempotent per day, so a retry after a refused send
doesn't age everything twice, and an item that vanished for more than a week and
came back starts again at day one (a deal that went quiet in March and stalled
again in August is on its first day of *this* problem).

### Empty day = no digest

If nothing is outstanding, nothing is posted. The bot never says "nothing to
report" — that is a message with no information in it, and posting one teaches
people the digest can be skipped. The once-a-day marker is deliberately *not*
written on an empty day, so something that turns up in the afternoon still gets
said that day.

### One message, even when it's long

If the body exceeds Discord's 2000-char limit it is split on line boundaries by
the same splitter the answer path uses and posted as **consecutive parts of one
digest** — `part 1 of 2`, `part 2 of 2`, back to back, in order, each audited
with its part number. Nothing is clipped: the per-section caps
(`SALES_DIGEST_MAX_PER_SECTION`, default 15; ESCALATIONS is never capped) have
already bounded the length, and they say how many items they left out rather than
dropping them silently.

### The tracker reminder is a **section**, not a message

Vaishnavi's twice-weekly *"update the tracker"* prompt goes out on
`TRACKER_REMINDER_DAYS` (**Mon and Fri** by default) as a **section of that day's
digest**, immediately after `UPDATE TRACKER`:

```
**UPDATE THE TRACKER — the twice-weekly check (3)**
@Vaishnavi
• Monday tracker check — update the outreach tracker: last followed-up date,
  total follow-ups, response, meeting date, next steps. 110 row(s) of 886 have
  no follow-up count or last-followed date, so no date rule can fire on them
  at all.
• The outreach tracker must be updated at the start and end of every week.
  (Sales Bot Discussion, 2 Sep)
• Update the tracker with last-followed-up dates for every open row.
  (Sales Bot Discussion, 2 Sep)
```

**There is no `_post_tracker_reminder` anywhere in the code, and there must
never be one.** The reminder cannot reach the channel except through the digest.
That is what keeps "exactly one unprompted message a day" a property of the code
rather than a promise — and it also means the reminder and the `UPDATE TRACKER`
section above it arrive together instead of as two prompts about the tracker
minutes apart, which is how a channel gets muted.

The count in the lead line comes from the cadence that ran seconds earlier
(`fill_in`), not from a second read, so the reminder can never disagree with the
section above it. The lines that quote a meeting carry **that meeting's
citation** — see [Citing meetings](#citing-meetings).

It posts even on a day with nothing else outstanding. That is the one exception
to *empty day = no digest*, and it is deliberate: the reminder is a real ask, so
it is worth the day's one message. Mechanically it is an item carrying
`forces_digest`, which `digest.total_items` counts but `_age_digest_items` never
ages — *"(3rd day)"* on a standing twice-weekly reminder would be nonsense.

### Audit trail for the whole one-message rule

Three send sites exist in `bot.py`, and only one of them is proactive:

| Where | What | Proactive? |
|---|---|---|
| `_post_digest` | the daily digest | **yes — the only one** |
| `_reply` | an answer to a question | no — someone asked |
| `_set_deadline_for` | the ask-time deadline announcement | no — someone asked, and it carries the "shout to change" consent |

Everything the bot has to say proactively is a **section** of the first one. If
you are adding something, add a section to `digest.py`; do not add a
`guardrails.send`.

### The exceptions

Two things are still immediate, because both are **answers rather than
interruptions**:

1. **A reply to a question.** Someone asked; the bot answers.
2. **The ask-time deadline announcement.** "Setting the follow-up for Emami to
   Wed 26 Aug … shout to change" is what someone just asked for, and the
   "shout to change" invitation is the consent mechanism — see
   [Deadline authority](#deadline-authority). Holding it for the next morning
   would answer a question a day late.

### Audit

Every digest writes one `daily_digest` line to `state/audit.jsonl` with the date,
the channel, the message id, the total item count and a per-section count
(`n_hot`, `n_deadlines`, `n_overdue`, `n_escalations`, `n_hygiene`, `n_funnel`,
`n_tracker_reminder`, `n_todos`, `n_plan`),
so an operator can see the shape of a day without reading the message. The
attempts and escalations the digest spends are audited individually too
(`chase_nudged`, `deadline_chase`, `chase_given_up`, `deadline_escalated`) — and
they are recorded **after** the send, so a refused digest never burns a chase
attempt.

### Settings

| Var | Default | What it does |
|---|---|---|
| `SALES_DIGEST_ENABLED` | `true` | `false` → the bot sends **no** unprompted messages at all. |
| `SALES_DIGEST_TIME` | `10:00` | Wall-clock IST (Asia/Kolkata, computed explicitly — never the server clock). |
| `SALES_DIGEST_MAX_PER_SECTION` | `15` | Items shown per section; the overflow is counted, not dropped. |
| `SALES_DIGEST_CHANNEL_ID` | unset | Must be in `SALES_CHANNEL_IDS`; unset → `SALES_ASK_CHANNEL_ID`, else the first sales channel. |
| `URGENT_MAX` | `25` | Hard ceiling on the urgent items (f, h, i). They are **never truncated** in practice — this only guards against a broken sheet. |
| `DIGEST_MAX_ITEMS` | `15` | **Non-urgent** cadence items carried, across all five sections combined. |
| `CONNECT_REMINDER_MAX_DAYS` | `30` | The cold ceiling: never-connected rows older than this are counted in one line instead of chased. `0` turns it off. |
| `UPDATE_TRACKER_MAX` | `5` | A **separate** budget for the "update the tracker" asks, so a sparse sheet's fill-in requests can't eat the fifteen. |
| `SALES_DEFAULT_OWNER_ID` | unset | Who every cadence line is addressed to, since the tracker has no owner column. Must also be in `TEAM_ROSTER_IDS` to be pinged. |
| `CADENCE_CROSSCHECK_ENABLED` | `true` | The nightly master/tracker consistency check. Reports disagreements; never picks a winner. |
| `CADENCE_DATA_QUALITY_ENABLED` | `true` | The sheet-health flags (broken formulas, misaligned master rows, stray response values), deduped until they change. |
| `TRACKER_REMINDER_ENABLED` | `true` | The twice-weekly tracker reminder **section**. It has no send path of its own, so `SALES_DIGEST_ENABLED=false` also silences it. |
| `TRACKER_REMINDER_DAYS` | `mon,fri` | Which days carry it. `mon`..`sun` or `0`–`6`, comma separated. |
| `TODO_REFRESH_DAY` | `fri` | Which day's digest refreshes the to-do sheet and carries its one line. |
| `STRATEGY_CHECK_ENABLED` | `true` | The weekly `AGAINST THE PLAN` block, on `WEEKLY_DIGEST_WEEKDAY`. A report, never an item — it can't make the digest post. |

### Seeing tomorrow's digest before it goes out

```
python -m main --dry-run-digest              # today
python -m main --dry-run-digest 2026-09-07   # a Monday
```

Builds the whole message for that day and prints it. It connects to nothing,
**sends nothing, creates nothing and ages nothing** — no Discord message, no
carry-forward ageing (so tomorrow's real digest isn't a day older than it should
be), no sheet created, no to-do row appended. This is how you check that the
Monday reminder really is a section of one message rather than a second message,
without waiting for a Monday.

The one side effect it cannot avoid is the cadence's next-step clock, which
counts days *observed* — the startup dry run already advances it for the same
reason.

---

## Citing meetings

> **Whenever meeting knowledge shapes a line — a hold, a decision, a commitment —
> the line names its source.**
>
> `"…on hold (Sales Bot Discussion, 2 Sep)"`

This applies everywhere: digest items, cadence chases, the tracker reminder, prep
briefs, the to-do sheet's **Source meeting** column, and answers to questions.
**A meeting-derived claim with no meeting citation is a bug**, not a style lapse,
and there is no setting that turns it off.

**Why it is a bug.** A sheet-derived line can be argued with by opening the sheet
— the bot already names the cell it read. A meeting-derived line has no such
handle: *"we're holding off on Acme"* is either something somebody actually
decided in a room, or something the bot inferred, and from the outside those two
are identical. The citation is what makes the difference visible.

`meetings.py` owns it. `meetings.cite(text, note)` writes the string, and it is
computed once and handed to every renderer (and to the model, in a `citation`
field on every notes tool result) rather than reconstructed at each call site —
which is how a line ends up uncited when one caller forgets.

### What is extracted

Only through `notes.list_notes` / `notes.read_note`, so a document the standup
exclusion holds back can never reach a digest line.

| Kind | From | What it changes |
|---|---|---|
| **hold** | a line saying work on a named company is paused, parked, deprioritised or not to be chased | the cadence line for that company gains *"— on hold (…)"*, and a prep brief for it opens with the hold |
| **decision** | the note's own *Decisions* section, verbatim | quotable in answers, and in the tracker reminder when it is about the tracker |
| **commitment** | the note's *Next steps* block | becomes a row on the to-do sheet |

**Nothing is summarised by a model.** Every fact is a line the team wrote,
carried through unchanged apart from clipping. A citation attached to a
paraphrase is worse than no citation: it lends a model's wording the authority of
a minute.

### A held company is still shown

Suppressing it would be the bot deciding a meeting outranks the pipeline, and the
row's owner would never learn why the row vanished. So the row stays, and the
line says what the meeting said and names it:

```
• OpenAI · Abhishek Garg (Product Manager) — last followed up 144d ago, still no
  response. Follow up or park it. — on hold (Sales Bot Discussion, 2 Sep)
```

### Two guards against a fabricated hold

**Companies come from the tracker.** A fact attaches to a company only when that
company's name — read off the outreach tracker, never invented — appears in the
line. The bot cannot announce a hold on a company that is not in the pipeline, or
mistake a person's surname for an account. Longest name wins, so *"Acme Research
Labs"* beats *"Acme"* on a line naming both.

**Negation kills a hold.** *"Anthropic stays active and is not paused"* contains
the word *paused* and means the opposite. Without the guard the bot would
annotate every Anthropic line with a hold the meeting explicitly ruled out —
worse than missing a real one, because it puts a fabricated decision in brackets
next to a genuine citation and lends it that citation's authority. Both
directions are checked: a negator in front of the phrase (*not*, *no longer*,
*nothing*, …) and an un-hold verb anywhere in the line (*resumed*, *off hold*,
*re-activated*, …).

---

## The to-do sheet

One Google Sheet — **Membrane Sales To-Dos** — created by the bot on first run,
shared with the team, and appended to weekly from the meeting notes.

| # | To-do | Owner | Source meeting | Date raised | Due | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Send the revised deck to Comet by Friday. | Vaishnavi | Sales Bot Discussion, 2 Sep | 2026-09-02 | 2026-09-04 | Open | |
| 2 | Confirm the pricing page copy by 12 Sep. | Kushal | Sales Bot Discussion, 2 Sep | 2026-09-02 | 2026-09-12 | Open | |

**The contract is asymmetric, and that is the point:**

* **the bot** appends action items it read out of the meeting notes;
* **humans** own Status and Notes entirely — the bot never writes to them, never
  edits a row and never deletes one.

Every write is a Sheets `append` (`drive.sheet_append`), so it is append-only *by
construction* rather than by convention: a write that could land on an occupied
row would silently discard somebody's edit.

### Sharing is the feature, not a nicety

A spreadsheet the service account creates is **owned by the service account** and
lives in a Drive no human can browse or search. **Until it is shared it is
invisible** — not "hard to find", invisible. So:

* creation and sharing are one operation (`todos.ensure`);
* share failures are reported **per address**, because "sharing failed" is not
  actionable and *"vaishnavi@… bounced: no such Google account"* is;
* the link is **posted in the sales channel** — as a section of the daily digest,
  so it is still one message a day;
* `todo_sheet` is a **source**, and its status line reports who can actually open
  the file rather than whether the API call worked.

`TEAM_SHARE_EMAILS` defaults to `trishi@nfthing.com`,
`vaishnavi@membrane.social`, `claudedrive@nfthing.com`, as **Editor**.

### The link is announced once, and the marker is persisted

The sheet is created at boot; the link goes out with the next digest, which may
be hours later and on the far side of a restart. Keying the announcement on *"did
I just create it"* would lose the link exactly when the bot was restarted between
the two — so `todo_sheet_announced` is stored in the `meta` table and the
announcement is spent as a **digest effect**, after the message actually posted.
A refused send leaves the link still to be announced tomorrow rather than
silently never.

The spreadsheet **id** is stored the same way (`todo_sheet_id`), written *before*
the share and the header row, so a failure in either leaves the next run
re-opening that sheet rather than creating a second one. `TODO_SHEET_ID`
overrides it, which is how you point the bot at a sheet somebody made by hand.

### The weekly refresh

On `TODO_REFRESH_DAY` (**Friday** by default), `meetings.action_items` reads the
last `TODO_NOTES_DAYS` of notes, each item carrying its owner, its **source
meeting** (the citation, as a column) and a **due date only if the line actually
states one** — *"by Friday"* resolves against the day it was raised, not against
today, so a to-do extracted a week late does not silently acquire a new deadline.

New items are deduped against **the rows in the sheet**, not against a bot-side
memory — that would drift the first time somebody deleted a row, and the item
would then never come back. The key is normalised task text plus owner: task
alone would merge *"[Vaishnavi] send the deck"* and *"[Kushal] send the deck"*,
which are two jobs.

That day's digest carries **one line**:

```
**TO-DO SHEET**
To-do sheet updated: +3 new · https://docs.google.com/spreadsheets/d/…/edit
```

Every append writes a `todo_appended` line to `state/audit.jsonl` naming the
task, the owner, the source meeting and the note it came from, plus one
`todo_refresh` summary — so a row in the sheet can always be traced back to the
meeting that produced it.

### Asking for it

*"@bot show the to-dos"* always replies with **the link and the open items**,
both, every time. The link alone is a shrug; the items alone leave the asker
unable to edit anything, and editing is the whole point of a sheet the humans
own. `todo_candidates` answers *"what came out of this week's meetings"* and is
**read-only** — asking what would go on the sheet must never trigger a write.

### It needs the Drive API enabled

Creating the sheet requires the **Google Drive API** to be enabled on the service
account's Cloud project — a one-time click in the console. Until it is, Google
answers with a bare `403 The caller does not have permission`, which reads as a
key, scope or sharing problem and is none of those. The bot creates the file
through the **Drive** endpoint rather than Sheets' own `spreadsheets.create`
precisely so the error carries Google's real message, and translates it into the
console URL that fixes it.

---

## The strategy doc

No longer a stub. `STRATEGY_DOC_ID` points at the **human-owned** strategy
document — Vaishnavi's edited version of the v2 draft — and the bot reads it
**read-only** over the Drive API (`drive.readonly`; there is no code path in
`drive.py` that writes to a file the bot did not create).

Three things run against it:

| | What | Where it shows |
|---|---|---|
| **cadence** | a cadence *stated in the doc* outranks the working-day defaults | every deadline announcement: *"4 working day(s) after the last touch, **per the strategy doc**"* |
| **currency** | Drive's `modifiedTime` against `STRATEGY_STALE_DAYS` | the weekly `AGAINST THE PLAN` block, the source status (which goes **degraded**), and before any answer that quotes the plan |
| **outreach vs plan** | the targets it names, against where outreach went | the weekly `AGAINST THE PLAN` block, and the `outreach_vs_plan` tool |

### How the targets are read, and the limits of it

The doc is prose written by a human, not a schema, so the extraction is
deliberately conservative. It takes the lines under a heading that says
**TARGET / ICP / SEGMENT / VERTICAL / PRIORITY**, and nothing else. Where the
section *ends* is the part that has to be right, because the next heading is
often a bare word on its own line that no heading regex catches — so the shape of
the list is the boundary:

* a **bulleted** list ends at the first non-bullet line;
* an **unbulleted** list ends at the first blank line.

Both fail closed: an early stop loses a target and the check simply says less; a
late stop would turn the *next section's title* into a target and then report
*"the plan names Cadence and nothing went to it"* — a finding about the parser
dressed up as a finding about the team.

**If the doc has no such heading, the bot says so** rather than guessing at a plan
from the whole text and reporting drift against its own guess. A wrong plan check
is worse than none: it sends the team to defend outreach against a target nobody
set.

### Both directions, with counts

```
**AGAINST THE PLAN**
Strategy last revised 2026-08-12 (21 days ago), past the 30-day rule. Outreach is
being checked against a plan nobody has touched since then.
Checked 885 row(s) touched 2025-07-29 to 2026-09-02 against 4 target(s) in the plan.
OFF-PLAN — outreach went to segment(s) the plan does not name: Founder / C-Suite
(244); Marketing & Growth (211); AI Labs - Frontier (138).
IN THE PLAN, NOTHING SENT — no outreach in this window matched: Quantum Computing.
```

Neither direction is an accusation — a plan can be out of date and the pipeline
right. Both are stated as the comparison they are, with the row counts behind
them, matched on the row's **own cells** (industry, vertical, use case) and never
on an inference about what a company "is".

**Zero outreach is reported as zero outreach.** On a week where nothing dated
went out at all, listing every target as neglected would read as a targeting
failure when it is a volume one, so the block says exactly that instead.

---

## State contract

Two files under `STATE_DIR` (default `./state`), written by this bot and intended
to be read by a supervising process (COSA). Treat them as a published interface:
new fields may be **added**, but existing ones are not renamed or repurposed
without updating this section, because something else is parsing them.

Both are gitignored — they are local operational state, not source.

### `state/summary.json`

A snapshot, **rewritten wholesale at startup and once a day** (at
`STATE_DAILY_HOUR`). Written to a temp file and atomically renamed, so a reader
never catches it half-written and can always assume valid JSON.

```jsonc
{
  "schema_version": 1,                  // bumped when a field changes meaning
  "bot_name": "SalesCoS",               // config.COS_NAME
  "generated_at": "2026-08-21T08:00:00Z",   // UTC, second precision, always 'Z'
  "trigger": "startup",                 // "startup" | "daily" — why this rewrite happened

  "scope": {                            // what the bot is allowed to touch
    "sales_channel_ids": ["123", "456"],// ids as strings (JS-safe)
    "ask_channel_id": "456",            // "" when unset
    "roster_size": 3,                   // how many people it may @-mention
    "never_dms": true                   // invariant, always true
  },

  "sources": [                          // one entry per source, fixed order
    {
      "key": "sales_spreadsheet",       // stable machine key
      "label": "the sales spreadsheet", // how the bot refers to it to a human
      "purpose": "pipeline, targets and the outreach log",
      "status": "awaiting-access",      // "connected" | "degraded" |
                                        // "awaiting-access" | "error"
      "detail": "I don't have access…"  // one actionable sentence
    }
  ],

  "open_chases": [                      // commitments still being waited on
    {
      "person": "Trishi",
      "what": "the Acme deck",          // quotable noun phrase
      "promised_at": "2026-08-20 14:03:00",  // UTC 'YYYY-MM-DD HH:MM:SS'
      "due_at": "2026-08-21 14:03:00",
      "reminders_sent": 1,
      "channel_id": "123",
      "jump_url": "https://discord.com/channels/…"
    }
  ],

  "notable_events": [                   // things a supervisor should look at
    { "kind": "source_unavailable",  "detail": "…" },
    { "kind": "source_degraded",     "detail": "…" },  // readable, going stale
    { "kind": "policy_missing",      "detail": "…" },
    { "kind": "channel_not_visible", "detail": "…" },  // role lacks View Channel
    { "kind": "nudges_last_24h",     "detail": "…" }
  ],

  "counts": {                           // derived; convenience for a dashboard
    "sources_connected": 1,
    "sources_awaiting_access": 2,
    "sources_degraded": 0,
    "open_chases": 1,
    "notable_events": 2
  }
}
```

### `state/audit.jsonl`

**Append-only**, one JSON object per line, never rewritten or reordered — safe to
tail. One line per action the bot took, each with a timestamp and a reason.

```jsonc
{"ts":"2026-08-25T04:30:11Z","bot":"SalesCoS","event":"daily_digest","reason":"the one scheduled proactive message of the day","date":"2026-08-25","channel_id":123,"message_id":789,"items":13,"n_hot":1,"n_deadlines":2,"n_overdue":4,"n_escalations":1,"n_funnel":3,"n_hygiene":5}
{"ts":"2026-08-25T04:30:11Z","bot":"SalesCoS","event":"chase_nudged","reason":"carried in the daily digest's OVERDUE section (attempt 1 of 2)","person":"Trishi","what":"the Acme deck","chase_id":4}
```

Guaranteed on every line: `ts` (UTC, `Z`), `bot`, `event`, `reason`. Everything
else varies by event type.

| `event` | Meaning |
|---|---|
| `startup` | the bot connected and rewrote its summary |
| `sheet_access_check` | startup probe of both GTM spreadsheets |
| `message_sent` | something was posted. Only three `kind`s exist now: `daily_digest` (the one proactive message, with `part` / `parts` when it was split), `reply` (an answer to a question) and `deadline` (the ask-time announcement). |
| `daily_digest` | **the digest went out** — with `items` and a per-section count (`n_hot`, `n_deadlines`, `n_overdue`, `n_escalations`, `n_funnel`, `n_hygiene`, `n_tracker_reminder`, `n_todos`, `n_plan`) |
| `send_refused` | **a guardrail blocked a send** — a DM attempt, or a channel outside the scope |
| `send_failed` | Discord rejected an otherwise-allowed send |
| `chase_opened` | a commitment was detected and is now tracked |
| `chase_closed` | they came back, or someone ✅'d the promise |
| `chase_nudged` | a chase was carried in the digest's OVERDUE section — one attempt spent |
| `chase_given_up` | the attempt cap was reached; it moved to ESCALATIONS and the owner stopped being chased |
| `deadline_set` | the bot set and announced a deadline |
| `deadline_adopted_human` | a person's date already existed; the bot took theirs |
| `sheet_write` | **every cell write**, with the cell, the value and whether it succeeded |
| `deadline_chase` | an overdue deadline appeared in the digest's OVERDUE section |
| `deadline_escalated` | the cap was spent; it moved to ESCALATIONS |
| `todo_sheet_created` | the to-do sheet did not exist and was created — with its id and url |
| `todo_sheet_shared` | who it was shared with, and **which addresses failed** |
| `todo_sheet_announced` | its link went out with the daily digest (spent once, after the send) |
| `todo_appended` | **one line per row appended** — the task, the owner, the `source_meeting` citation, the date raised, the due date and the note file it came from |
| `todo_refresh` | the weekly refresh summary: `considered`, `added`, `skipped` |

Retired with the individual sends they recorded: `deadline_reminder`,
`deadline_escalation`, `row_flag`, `weekly_digest`. Old lines with those events
stay in the log — it is append-only — but nothing writes them any more.

`send_refused` is the line to alert on: it means code attempted something the
guardrails forbid.

---

## Layout

| File | Role |
|---|---|
| `main.py` | entry point; validates env, logs scope and source statuses, connects. `--dry-run-digest [YYYY-MM-DD]` prints a day's digest and sends nothing |
| `bot.py` | the Discord client: routing, question answering, chasing, and the one daily digest — its only proactive send |
| `digest.py` | the daily digest: sections, per-owner grouping, carry-forward ages, rendering |
| `cadence.py` | **the ten phase-1 rules (a–j)**, exclusion, the missing-data routing, the master/tracker cross-check, the sheet-health flags, priority, the two caps, and the dry run. Pure: rows in, dicts out — no sheet reads, no writes, no Discord |
| `prep.py` | the meeting-prep brief: tracker row + mapping (with caveats) + positioning + **cited** notes, an opening hold line when a meeting parked the account, and the explicit "no web access" section |
| `meetings.py` | **the meeting knowledge layer and the citation rule**: holds, decisions and commitments read out of the notes, each carrying `"<meeting>, <date>"`. Pure and send-free |
| `todos.py` | **Membrane Sales To-Dos**: create, share, header, weekly extract + dedup + append, the digest's one line, and the "show the to-dos" answer. Append-only; no Discord |
| `strategy.py` | the strategy doc: read-only Drive reader, currency (stale-doc) and the outreach-vs-plan check |
| `drive.py` | the **second** Google credential — Drive + Docs + the Sheets REST calls for the bot's own sheet. Wider scopes live here so `gtm_sheet.py` keeps its narrow one |
| `guardrails.py` | **the hard rules** — every send and every read passes through here |
| `config.py` | environment → typed settings, with loud warnings for likely mistakes |
| `persona.py` | the voice, plus loading `sales_policy.md` fresh on every question |
| `sales_policy.md` | the operating policy — the eleven principles |
| `sources.py` | the five sources and their connected / degraded / awaiting-access status |
| `gtm_sheet.py` | the Sheets API layer: auth, schema discovery, cached reads, the narrow write path |
| `mapping_sheet.py` | the researcher/buyer mapping — **read-only**: no write method, read-only scope, and the legend loaded as enforced rules |
| `tracker.py` | the tracker read as a pipeline: the three flags, the playbook's six-stage funnel definition, and the week's leading/lagging metrics |
| `deadlines.py` | IST working-day maths, cadence resolution, the announcement |
| `notes.py` | syncs the Drive meeting notes into `NOTES_DIR`, filters them to the sales ones, reads those |
| `query.py` | Discord read primitives, scoped to the sales channels |
| `query_engine.py` | the bounded tool-use loop; holds no tools of its own |
| `llm.py` | the short model calls: routing, replies, commitment detection |
| `followups.py` | commitment prefilter, due-time maths, fallback nudge text |
| `db.py` | SQLite: `chases`, `nudges`, `deadlines`, `flags_sent`, `digest_items` (carry-forward ages), `nextstep_state` (rule (i)'s clock), `prep_briefs` (one brief per meeting), `meta` (the once-a-day digest marker, the to-do sheet's id and its announcement marker) |
| `memory.py` | short-term per-channel conversation memory (in-memory only) |
| `state.py` | writes the state contract above |
| `ecosystem.config.js` | PM2 process definition (`sales-bot`) |

---

## Design notes

**Why the engine has no built-in tools.** `QueryEngine.answer(tools=…)` takes its
complete tool set from the caller. It holds no credential and no data source, so
it cannot reach anything `bot.py` didn't hand it — which is what makes the channel
scoping hold on the query path too.

**Why chases are capped.** The bot posts unprompted @-mentions, and the cap is now
structural rather than clock-based: an "attempt" is an appearance in the daily
digest, and the digest speaks once a day, so nothing can be chased more than once
a day by construction. `COS_NUDGE_MAX_ATTEMPTS` still bounds the total, after
which an item moves to ESCALATIONS and its owner stops being chased. The `nudges`
table is still written for the audit trail; attempts are recorded **after** the
digest posts, so a refused send never burns one.

**Why one message a day, and why it's structural.** Six kinds of proactive
message spread through a working day is a drip, and a channel that drips gets
muted — at which point every rule in this README is decorative. The consolidation
is therefore not a setting: the individual reminder, chase, escalation and flag
send paths were deleted, and `_maybe_post_daily_digest` is the only proactive
send left in `bot.py`. Adding a new thing to say means adding a section in
`digest.py`, not a `guardrails.send`.

**Why the mapping sheet is locked three ways.** One lock is a promise; three are
a guarantee. The scope lock and the id-refusal lock are each individually
sufficient, and they fail in different directions — a code mistake can't defeat
the scope, and a misconfiguration can't defeat the code. The cost is a second
gspread client holding a narrower token, which is a few bytes and no extra
network calls.

**Why the sheet's rules live in the tool results, not only the prompt.** A rule
stated once in a system prompt loses to a row of data quoted in a tool result ten
turns later. So every mapping result carries its `rules` block, and every row
carries its own caveats inline — `do_not_recommend`, `do_not_pitch`,
`staleness.caveat`, `watch_outs`. The rule and the fact it constrains arrive
together or not at all.

**Why "I can't see that yet" is a feature.** The failure mode for an assistant
with a source missing isn't silence, it's a confident answer built from the ones
it has. The source statuses are in the system prompt, the prompt forbids
answering from an unreachable source, and the capability answer has a
deterministic fallback (`persona.fallback_capability_reply`) so it stays honest
even when the model call fails.

---

Scaffolded from the PM bot (`discord--linear-bot`), with the issue-tracker
integration and the whole triage-to-ticket pipeline removed.
