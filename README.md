# Sales & Marketing CoS bot

A Discord bot that acts as a sales & marketing chief of staff for the NFThing
team. It reads the sales channels, answers questions from what it can actually
see, and chases the deadlines people commit to in passing.

**It speaks unprompted at most three times a weekday.** Everything it has to
raise goes out as a [drip](#the-drip--a-few-short-messages-a-day): short,
time-spaced messages, **one per (action type × owner)**, first at
`SALES_DRIP_START`. Everything else it says is a reply to a person, and replies
are immediate.

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

**Reads one canonical tab: "Outreach PoCs".** Everything the bot says on its own
initiative comes from that tab of the GTM Playbook, found **by name**
(`GTM_POCS_TAB_TITLES`) with its columns discovered dynamically. A row is
**ACTIVE** only when a first-contact date or a connection date is present in it,
and an inactive row is invisible to every proactive feature — never mentioned,
chased or counted. Ask about one by name and it still answers in full. See
[Row activation](#row-activation--which-rows-the-bot-may-raise-unprompted).

**Never writes outside a narrow window.** `RESTRICTED_COLUMN_RANGES` (default
`A:I,S:X`) names bands of columns denied to every write path in the code; the
named columns in the writable window between them are logged at startup so a
shifted column is visible before a write lands in the wrong place. Reading is
unrestricted. See [Writes](#writes--deliberately-tiny-and-locked-to-a-window).

**The phase-1 cadence rules (a–j) are retired.** They ran on the old outreach
tracker tab, which is retired with them. Their evaluation is removed, not
disabled — the digest's cadence sections carry sheet-health lines only until a
phase-2 rule set exists, and the log says so rather than going quietly empty. See
[The cadence](#the-cadence--the-phase-1-rules-are-retired).

**Chases deadlines — and sets them.** When someone says "I'll send Acme the deck
tomorrow" in a sales channel, that becomes a *chase*. When a deal has **no**
deadline, the bot sets one itself, announces it with the rule it used, and invites
anyone to change it (see [Deadline authority](#deadline-authority)). Once a chase
is overdue it is **recorded** — the promise, the age, the attempt count, the
escalation threshold — and answerable on demand. The digest's OVERDUE and
ESCALATIONS sections that used to announce it are retired with the digest, and
the drip does not yet carry chases: it groups the next-action queue, and a chase
is not an action type on it.

**Decides one next action per row.** A state machine turns each active row into
**exactly one** thing to do — type, owner, due date, priority — or nothing, with
a reason. Positive replies jump the queue, closed rows stop for good, snoozes are
honoured, and no due date lands on a weekend. It **sends nothing**: read it by
asking for `cadence preview`. See
[The next-action state machine](#the-next-action-state-machine).

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

Before a deploy, `python -m main --dry-run-drip 2026-09-07` prints that day's
whole plan — how many messages, to whom, about what, at what times — and **sends
nothing**. `--dry-run-digest` still works as an alias.

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
| `sales_spreadsheet` | **wired up** (Sheets API) | the GTM Playbook: the canonical **Outreach PoCs** tab, plus master data, positioning matrix, prospect priority |
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

### One spreadsheet

| | Sheet | Role |
|---|---|---|
| `GTM_SHEET_ORIGINAL_ID` | *NFThing <> GTM Playbook* | read **and written** — see [Writes](#writes--into-the-real-sheet-now-inside-one-window) |

`GTM_SHEET_COPY_ID` is gone with the sandbox era.

### Auth, and the one setup step people miss

`GOOGLE_SERVICE_ACCOUNT_JSON` points at a service-account key file. Creating the
key is not enough — the playbook must be **shared with the service account's
address as EDITOR**, and the mapping sheet as *Viewer*.

**Editor, not Viewer, and that is a change.** The bot writes into the real
playbook now. A Viewer share reads perfectly and fails on the first write, which
is a bad thing to discover on the day somebody replies to a nudge.

Without that, every read is a `403` and the bot reports the spreadsheet as
awaiting-access. It does not crash, and it does not answer sheet questions from
guesswork; it logs the exact line to fix:

```
[bot] GTM Playbook NOT reachable: the service account cannot open the original sheet (permission denied)
[bot] ACTION REQUIRED: Share "NFThing <> GTM Playbook" with sales-bot@… as Editor (open the sheet, click Share, paste the address, set Editor, Send).
```

The key file is a secret: `.gitignore` covers the usual key filenames. If one is
ever committed, **revoke it in Google Cloud** — deleting the file is not enough.

### The canonical tab: "Outreach PoCs", found **by name**

**The bot's sheet world is the "Outreach PoCs" tab of the GTM Playbook.**
Everything it says on its own initiative comes from that tab and no other. The
spreadsheet is unchanged — `GTM_SHEET_ORIGINAL_ID` still points at the same
playbook.

**It is found by NAME, not by header signature**, which is the exact opposite of
how every other tab is found — and it is deliberate. The retired outreach
tracker tab carries near-identical columns, so a header signature would happily
re-adopt it as the canonical tab and undo the move. Naming the tab is what makes
the retirement real.

```bash
GTM_POCS_TAB_TITLES=Outreach PoCs,Outreach POCs,Outreach PoC,Outreach Pocs
```

If no tab has that name, the bot logs at **ERROR**, lists every tab title it did
find, and every proactive feature has nothing to run against. That is the honest
failure: it never quietly falls back to another tab.

**Its columns are still discovered dynamically.** Nothing about the tab's header
text is compiled in — headers are read at parse time, matched to roles by alias,
and anything unmatched is carried as `_extra` so a question about a column this
code has never heard of is still answerable. The **full discovered schema of
every tab** is logged at startup (`GTM_LOG_FULL_SCHEMA`, on by default), and the
canonical tab's schema is printed again in the boot report with each column's
letter, header, role and whether it falls inside a restricted band.

#### The old tracker tab is retired

The hidden "Outreach Updates" tab and the phase-1 cadence rules that ran on it
are **retired**. It is no longer recognised as any kind, nothing evaluates
against it, and no proactive output path is wired to it. See
[The cadence](#the-cadence--the-phase-1-rules-are-retired).

### The other tabs, identified by header signature

Every tab apart from the canonical one is recognised **by the columns it
carries, never by its name** — names in this playbook drift, signatures don't:

| Kind | Signature (all required) | Live tab, 1 Sep |
|---|---|---|
| `outreach_pocs` | **matched by NAME** (`GTM_POCS_TAB_TITLES`) | **"Outreach PoCs"** |
| `master_data` | `Response Status` + `Intro Sent` + `Meeting Done` | **"Master Data"** (587 rows) |
| `lead_pipeline` | `Lead Stage` + `Estimated Value (INR)` | **"Lead Master Sheet"** (hidden, 333 rows) |
| `funnel_pivot` | `Vertical / Stage` | **"Sales Funnel - March-June 2026"** (hidden) |
| `researcher_lines` | `Outreach Line - Researchers` + `Dates` | **"Master Pipeline"** (271 rows) |
| `positioning_matrix` | `Use Case` + Problem / Offering / ICP / Business Impact | **"Sales Outreach Matrix"** (hidden) |
| `prospect_priority` | `Company` + `Priority` (+ score / rationale) | Fortune 500 and AI-agent lists |

The master tab is status-only: Connected / Intro Sent / Response Status /
Meeting Done / Assets Shared and a `Month` that is a month, not a date. It
answers aggregate questions and defines the weekly funnel.

**Which real tab got which role is logged at startup**, so that question is one
log line rather than a guess:

```
[gtm.roles] outreach_pocs     OUTREACH PoCs — THE CANONICAL TAB (found by name)      -> 'Outreach PoCs' (886 rows)
[gtm.roles] master_data        MASTER — status only (aggregates, funnel, cross-check) -> 'Master Data' (587 rows)
[gtm.roles] lead_pipeline      PIPELINE — lead stage / estimated value                -> 'Lead Master Sheet' (333 rows, HIDDEN)
[gtm.roles] funnel_pivot       FUNNEL PIVOT — the funnel stage definition             -> 'Sales Funnel - March-June 2026' (38 rows, HIDDEN)
[gtm.roles] researcher_lines   RESEARCHER LINES — outreach lines for researchers      -> 'Master Pipeline' (271 rows)
```

**Hidden tabs are read** (`worksheets(exclude_hidden=False)`). The canonical
tab is taken by name whether it is hidden or not.

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
GTM_COLUMN_MAP={"outreach_pocs":{"company":"Client Name","owner":"Owned By"}}
```

The two roles worth checking first are **`first_contacted`** and
**`connected`** on the canonical tab: they are the *activation* columns, and a
row is invisible to every proactive feature unless one of them holds a date. If
the startup log shows almost no active rows, check that those two mapped to the
right headers before checking anything else.

A one-line schema summary is logged per tab at startup:

```
[gtm] original tab 'Outreach PoCs' kind=outreach_pocs rows=886 cols=23 mapped=[assets_shared, company, connected, first_contacted, …]
```

### Row activation — which rows the bot may raise unprompted

**A row of the canonical tab is ACTIVE only when a first-contact date OR a
connection date is present in it.** An **inactive** row is invisible to every
*proactive* feature: never mentioned, never chased, never counted.

That covers the next-action queue, the daily digest's cadence sections, the
weekly funnel numbers, the outreach-vs-plan check, the company list the meeting
layer is allowed to name, and the twice-weekly tracker reminder's counts.

**Why.** The tab is a working list of people somebody *might* contact, not a
list of people somebody *has* contacted; most of it is research — a name, an
organisation, a designation, and nothing else. A bot that chases those rows is
not chasing outreach, it is chasing a spreadsheet, and it buries the handful of
rows actually in flight under two hundred that were never started. The dates are
the sheet's own record of *"this one is real"*, so they are the signal and
nothing else is.

**A cell that is not a date does not activate.** `Yes` in a connection column is
somebody's shorthand, not a record of *when*. Any of the sheet's own date
formats counts, including a cell holding two dates (a meeting that moved) — the
latest readable one wins.

**Reading is unaffected.** Ask about an inactive row by name and you get the
whole row, including *"we have no record of contacting them"*. The rule governs
what the bot brings up on its own initiative, not what it is allowed to know.
`lookup_company` and `query_tracker` read the tab unfiltered.

**Rejection is a second, narrower gate.** Activation asks *"has this ever
started"*; `CADENCE_REJECTED_MARKERS` asks *"was it deliberately stopped"*. They
are counted and reported separately, because *"we never contacted them"* and
*"they said no"* are not the same answer to any question.

#### The one exception: an explicit mention-request

> *"Set connection reminders for the others at Acme."*

That activates those rows by name. It is a person deciding those rows are real —
exactly the judgement the date columns were standing in for.

- The activation is **persisted in SQLite** (the `row_activations` table), so it
  survives a restart. A bot that forgot, on the next reboot, an instruction it
  was given out loud would be worse than one that never took it.
- It is keyed on **company + PoC, never on the sheet row number**, which moves
  the moment somebody sorts the tab and would silently transfer the activation
  to whoever landed in that row next.
- **Nothing is written to the spreadsheet.** This is the bot's own record.
- A name that matches no row comes back as `not_found` rather than being
  silently dropped — *"I activated them"* when one of the three people named
  doesn't exist in the sheet is the kind of quiet inaccuracy that gets a bot
  distrusted.

#### Checking it: "sheet status"

```
you: @bot sheet status
```

Returns the canonical tab name, the total rows, the **active** rows, the
inactive count, any explicit activations in force, the restricted bands, and the
**named columns inside the writable window**. `886 rows, 12 active` is the
honest answer to *"why has the digest gone quiet"* — and it is an answer nobody
could give while the only visible number was the row count.

The same four facts are logged at boot under `[sheet.world]`, so they are
visible on the first restart rather than a week later.

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

### Writes — into the real sheet now, inside one window

**The sandbox-copy era is over.** `GTM_SHEET_COPY_ID`, `SHEET_WRITE_TARGET` and
`BOT_DEADLINE_COLUMN` are removed; setting any of them does nothing.

**What they were.** The bot owned one column — `Next Deadline (bot)`, appended at
the far right of the tab — and wrote single cells into it, on a **sandbox copy**
of the playbook by default. Around it sat `ensure_bot_column()`,
`write_deadline_cell()`, `_row_mismatch()`, `read_cell()` and
`verify_write_roundtrip()`. All removed.

That was the right shape while nobody had agreed what the bot may touch, and it
was useless for exactly as long. A column on a copy nobody opens is a write into
a drawer. The team works in the real sheet, and the two were only ever
row-aligned by luck — which is why every single write had to re-check the company
name first.

**What replaces it**, per Vaishnavi's walkthrough: `write_cells()` — cells in the
**real** Outreach PoCs tab, in the columns the team already keeps, and only
inside the **writable window** between the restricted bands.

> The service account now needs **Editor**, not Viewer. A Viewer share reads
> perfectly and fails on the first write, with a 403 the bot translates into
> *"shared read-only, needs Editor"*.

### Two triggers, and nothing else writes

| | |
|---|---|
| **(a) Reply** | a team member replies to one of the bot's own messages |
| **(b) Command** | a team member @-mentions it with an instruction — *"update Sahaj's meeting to Friday"* |

No scheduled writes. No inference from a passing remark in the channel. A cell
changes because a person addressed the bot and said something that answers what
that cell holds.

A reply is resolved against the nudge it answers: `drip_sends` records the
Discord message id and the companies of every drip message, so *"sent this
morning"* — which names no company and no column — resolves to exactly one row.
**If a company matches more than one row, the write is refused with a question**
rather than applied to whichever came first.

### The fill rule

> An **empty** cell is filled.
> A **non-empty** cell changes only when the reply **clearly supersedes** it.

That asymmetry is the whole design. Filling a blank costs nothing if it is wrong
— the cell was empty and the echo shows what went in. Overwriting a value
somebody typed destroys information, so it needs the reply to actually say the
new thing (*"moved to Friday"*, *"actually it went Tuesday"*), not merely to
mention the subject. The extractor sets `supersedes`; `sheetwrite.plan_writes`
enforces that nothing without it can overwrite.

### Three tiers

| Tier | Columns |
|---|---|
| **Reply-loop** | first-contact type, first-contact date, connection date, DM-sent date, Responded?, meeting status, meeting date, next steps, notes, package sent, assets shared, prospect status |
| **Command-only** | closure probability, deal size, deal status |
| **Never** | anything in a restricted band (`A:I`, `S:X`) |

Command-only exists because those are **commercial judgements**. Somebody
mentioning a number in a sentence is not somebody committing it to the sheet, and
the difference between those two things is a forecast nobody agreed to.

**The bands outrank the tiers**, and that has a consequence worth knowing: if a
command-only column physically sits inside a restricted band on the real sheet,
**no instruction can write it** and the bot says so by name. That is the correct
precedence — the bands are a promise about what the bot cannot touch at all, and
a promise a sufficiently explicit instruction could override would not be one. To
let the bot maintain those columns they move into the window, or the bands
change; both are decisions somebody makes on purpose in `.env`.

**Dead and Unresponsive need the actual words.** Those values stop a row for good
in the [next-action engine](#the-next-action-state-machine), so the bot never
infers one — the message has to contain one of `TERMINAL_STATUS_WORDS` verbatim,
matched against the **raw text**, not against the model's reading of it.

**A ceiling of `SHEET_WRITE_MAX_CELLS` (4).** One sentence should touch one or
two cells; an extraction that wants nine has misread something. Past the ceiling
the write is refused **whole** — never half-applied — and the bot asks for one
thing at a time.

### Contact details are acknowledged, never written

Somebody replies *"sure, her email is ann@acme.com"*. The email column is in the
identity band.

```
Noted — I set Acme · Ann's next steps to send the deck. I have got the email
(ann@acme.com) — that column is one I never write to, so could you drop it in
yourself? Say undo any time in the next 24h and I will put it back.
```

Refusing silently would lose the information; writing it would break the one
guarantee the bands exist to make. Email, phone and LinkedIn have **mapped
roles** precisely so the bot can name the column it is declining, rather than
saying *"I have no rule for that"*.

### Echo and undo

**Every write is echoed in one friendly line**, in the drip's voice, naming the
columns as the sheet names them and always offering the undo. A write nobody was
told about is a write nobody can catch.

**`undo` reverts the exact cells**, within `SHEET_WRITE_UNDO_HOURS` (24), **by
any team member** — not just whoever caused it, because the person who spots a
wrong cell is usually not the person who typed the sentence that produced it.

The prior value of every cell is stored in `sheet_writes` when the write happens,
so the restore is exact rather than a guess. Rows are marked `undone_at` rather
than deleted: a write that was made and reversed is a different history from one
that never happened, and `audit.jsonl` carries both events.

**The undo is a write like any other** and goes through the same three locks. An
undo that skipped the band check would be a way to write anywhere by writing
there first and then "undoing" something else. The row interlock matters more
here than on the way out: the undo may be a day later, and restoring an old value
into whatever now sits in that row would be a second, worse mistake dressed as a
correction.

### The three locks, still

1. **Read-only sheet ids** — never the mapping sheet, whatever the config says.
2. **Restricted bands** — never a column in `A:I` or `S:X`. Fails closed: a
   column index that cannot be read as a number is refused.
3. **The row interlock** — the target row must still name the company the caller
   believes it does. Rows get sorted and inserted between a read and a write, and
   a correct value in the wrong row is worse than no value, because nobody goes
   looking for it.

### Snooze parsing rides here too

> *"follow up in 15 days"* · *"on the 24th"* · *"remind me Saturday 6pm about the pilot"*

**The kind is decided by whether they gave a time.** *"In 15 days"* is a cadence
instruction and goes to the `snoozes` table, where the next-action engine re-arms
the due date to it. *"Saturday 6pm"* is somebody asking to be reminded at a
moment — that is `scheduled_reminders`, the one thing exempt from the weekend
shift. Collapsing those would either move somebody's Saturday or turn a soft
*"sometime in a fortnight"* into an alarm.

Parsed by regex rather than by the model: a date is a thing a regex can be held
to, and a snooze quietly entered for the wrong day would make the bot go silent
about an account for reasons nobody could reconstruct. Confirmed once, in the
plan's voice:

```
Got it — I will bring Acme back up on Sat 12 Sep at 6pm. Nothing from me on it
before then.
```

### SQLite is still the brain

The sheet is what the **team** reads; SQLite is what the **bot** knows. Deadlines,
snoozes, scheduled reminders, activations, the drip's slot log and the write/undo
history all live there and are authoritative. A failed sheet write loses nothing
the bot needed.

Deadlines are no longer mirrored to a sheet at all — there is no bot-owned column
to copy them into, and inventing a place inside the team's own window would be
the bot deciding which of *their* columns means "the bot's deadline".

#### Verifying the write path

```bash
python sheetwrite.py          # the tiers, the fill rule, the gates, the parsing — offline
python -m gtm_sheet           # access check, schema dump, and the writable window by name
python -m gtm_sheet --write --row 14 --role next_steps
```

`--write` writes one real cell and **puts the old value straight back**. It
refuses unless given **both** `--row` and `--role`: there is no sandbox to hide
in any more, so it will not pick a row to scribble on for you.

`python sheetwrite.py` runs 34 assertions offline — that an empty cell is filled
and a full one is not, that `supersedes` is what unlocks an overwrite, that a
reply cannot set closure but a command can, that an email becomes an ask naming
the sheet's own column, that Dead is refused until somebody says the word, that a
role with no column is named rather than silently dropped, and that going over
the ceiling writes nothing at all.

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
   `GTM_SHEET_ORIGINAL_ID` at this sheet by mistake and the write is refused with
   an explanation; `config.validate()` additionally forces
   `SHEET_WRITES_ENABLED=false` and says so at ERROR.

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

The bot never sets deadlines **unprompted**, and it no longer announces them
unprompted either.

**Deadlines are still tracked in full** — the date, the rule that produced it,
whether a human's date won, the attempt count, the escalation threshold — and
they are answered on demand (`list_deadlines`, `set_deadline`) and mirrored to
the sheet within the [restricted bands](#writes--deliberately-tiny-and-locked-to-a-window).

**What is gone is the announcing.** The DEADLINES / OVERDUE / ESCALATIONS
sections were the digest's, and the digest is retired; the drip carries the
next-action queue, where a chase is not an action type. Re-surfacing them means
deciding who a chase message is *for* under the one-type-one-owner rule, which is
a product question rather than a config one.

---

## The next-action state machine

**For every ACTIVE row, exactly one next action** — a type, an owner, a due date
and a priority — **or nothing at all, with a reason.** That is the whole
contract.

It **replaces** the per-row *"what next"* logic that came before it: the three
row-hygiene flags (HOT / STALLED / DEAD-DEAL) in `tracker.py`, and the ad-hoc
deadline kinds in `deadlines.py` that stood in for a cadence. Between them a
single row could be HOT *and* DEAD-DEAL *and* carry an outreach deadline, and
`tracker.all_flags` held an ordering to pick which of those to say out loud. That
is a system with three opinions and a tie-break, not an answer.

> **It sends nothing.** `nextaction.py` is pure computation — rows in, dicts out.
> It has no send path, it is **not** wired into the daily digest, and the only
> ways to see it are asking the bot (`cadence preview`) or the startup log. It is
> safe to deploy with the digest kill switch off, which is what it was built for.

### The triggers, in evaluation order

**The first match wins.** The order *is* the product decision: a row with a
positive reply, a pending demo quote and four silent touches gets the meeting
proposal, because that is what a human would do first. Nothing downstream has to
break a tie, because no tie is ever produced.

| # | Trigger | Fires when | Band |
|---|---|---|---|
| 0 | *stopped* | closure `0%` / Dead / Unresponsive / Won / Lost | **STOP** — no action, ever |
| 1 | *snoozed* | a live snooze | silent until its date |
| 2 | `meeting_proposal` | **any** positive or replied row | P0 override |
| 3 | `scheduled_reminder` | a one-off somebody asked for, due | P1 |
| 4 | `quote_chase` | stage = Demo, nothing moved, `DEMO_QUOTE_DAYS` (3) | P1 |
| 5 | `progress_check` | `dm_sent_date` + `DM_PROGRESS_CHECK_DAYS` (7) | P2 |
| 6 | `dm_check` | connection + `CONNECTION_DM_CHECK_DAYS` (7), still no DM | P2 |
| 7 | `dm_sent_check` | connection + `CONNECT_DM_CHECK_HOURS` (48) , no DM | P2 |
| 8 | `mark_unresponsive` | silent touches ≥ `UNRESPONSIVE_SUGGEST_AT` (7) | P2 |
| 9 | `channel_switch` | silent touches ≥ `CHANNEL_SWITCH_AT` (4) | P2 |
| 10 | `pulse_check` | deal On Hold, `ON_HOLD_PULSE_DAYS` (30) apart | P3 |
| 11 | `followup` | the lane cadence, off the last touch | P1 / P2 / P3 |

### Priority within a day

```
P0  positive overrides      a reply is the most expensive thing to sit on
P1  meetings & demo chases  a booked thing, or a quote somebody is waiting for
P2  seven-day follow-ups    the ordinary cadence
P3  slow lane               closure below the threshold, and parked deals
```

Two rows in the same band are ordered by **due date, oldest first**, then by
company so the list is stable day to day. Band beats date deliberately: a reply
due tomorrow outranks a slow-lane follow-up that went overdue three weeks ago,
because the reply is the thing that decays.

### The rules that need saying out loud

**Stop means stop.** A row whose closure cell says `0%`, Dead, Unresponsive, Won
or Lost produces no action, ever, from any trigger — **the priority override
included**. A won deal does not need a meeting proposal and a dead one does not
need a follow-up. A **blank** closure cell is *not* a stop: most rows have never
had the column filled in, and reading a blank as 0% would silence the whole
sheet on the first run.

**The priority override** (strategy §5.1) is the one band that jumps everything.
Any row that replied — positively **or unclassifiably**, because somebody wrote
*something* in the cell so they did answer — gets a meeting proposal due within
`MEETING_PROPOSAL_WORKING_DAYS` **working** days of the reply. A reply sitting
for a week produces a date **in the past**, and it is left there: the queue shows
it as overdue, which is the true statement. Nothing is quietly restamped "due
today".

**The type-aware ask** is the only place first-contact type is read. The progress
check asks for an email address or a phone number — unless the first contact was
already by email (`EMAIL_CONTACT_TYPES`), in which case we have the email and
asking for it reads as a bot that does not read its own sheet. A blank or
unreadable type **includes** the ask, which is the safe direction.

**The two lanes.** Closure above `CLOSURE_HOT_THRESHOLD` (50), or a deal marked
In Progress, is chased every `HOT_DEAL_DAYS` (7) near the front of the queue.
Closure at or below it is chased every `SLOW_LANE_DAYS` (20) at the back. **On
the threshold counts as slow** — "50%" is not "more likely than not", and a
coin-flip deal does not earn a weekly chase.

**On hold means don't chase.** The pulse-check trigger sits **above** the
follow-up, so no chase, channel counsel or unresponsive suggestion can reach a
parked deal. It gets one check-in a month and nothing else.

**The bot never marks anyone Unresponsive.** It suggests it, and says so on the
line. Marking somebody unresponsive stops the row for good — trigger 0 takes it
from then on — and a judgement with that consequence belongs to whoever owns the
row, not to a counter.

**Alternate PoCs.** Once a row has been followed up at least once with nothing
back, the follow-up line reads *"…if they stay quiet, try a different PoC at
&lt;company&gt;"*. It stays **one** action; the suggestion is part of its text.

### Snooze

> *"Follow up in 5 days."* · *"Come back to Acme on the 20th."*

A snoozed row emits **nothing** until its date. On and after it, the row's normal
action returns with its **due date re-armed to the snooze date** rather than to
whatever the trigger would have computed — the person who said *"the 20th"* said
when, and a bot recomputing a different date would be overruling them.

A snooze whose date has passed is therefore **overdue, and stays visible** until
somebody acts on it. It is not silently dropped: an instruction that expires into
nothing is an instruction the bot took and then ignored.

Stored in the `snoozes` table, keyed on company + PoC (never a sheet row number,
which moves when anybody sorts the tab). Set it with the `snooze_row` tool; it
writes to SQLite and **never** to the spreadsheet.

### Weekends

**No computed due date ever lands on a Saturday or a Sunday** — each is shifted
forward to the Monday (`NEXT_ACTION_WEEKEND_SHIFT`).

**The one exception is an explicitly scheduled reminder**: a one-off somebody
asked for at a specific time (*"remind me about Acme on Saturday morning"*).
Those live in the `scheduled_reminders` table and keep the **exact** date they
were asked for. A person asking for their own Saturday has decided about their
own Saturday, and a bot that "corrects" it to Monday has thrown the instruction
away without saying so. Set one with the `schedule_reminder` tool.

A scheduled reminder outranks the row's ordinary follow-up but **not** a positive
reply — the override still comes first.

### `cadence preview`

Mention the bot and ask for **"cadence preview"**. It computes today's queue and
prints it grouped by **type × owner**, and **sends nothing**:

```
**Cadence preview — Wed 09 Sep 2026**
_8 action(s) across 11 active row(s). Nothing has been sent; this is what the
queue holds right now._

__POSITIVE OVERRIDE (1)__
**Propose a meeting (they replied)** (1)
  _Vaishnavi_
    • [2026-08-04 — 36d OVERDUE] Acme · Ann (CTO) — they replied (P). Propose a
      meeting. This is front of the queue however old anything else on the row is.
      why: response cell says 'P'; due 2 working day(s) after the first contact
           date (Sat 01 Aug 2026)

__MEETINGS & DEMO CHASES (2)__
**Quote chases (demo given)** (1)
  _Vaishnavi_
    • [2026-08-24 — 16d OVERDUE] Fathom · Fay (CTO) — demo done 20d ago and
      nothing has moved since. Send the quote, or write down what is blocking it.
      why: stage is 'Demo', nothing recorded since the first contact date
           (Thu 20 Aug 2026), and 3d have passed
…
_No action for 2 closed (0% / Dead / Unresponsive / Won / Lost) — never chased
again; 1 snoozed._
```

Grouped by type first because the question behind it is *what kind of work is
waiting*; owner is the sub-grouping so each person can still find their own rows.
Every line carries its due date, whether it is overdue, and **the cells that
produced it** — a queue nobody can check is a queue nobody will act on.

**The closing line is the receipt.** Every active row that produced *no* action
is accounted for by category — stopped, snoozed, no readable date, nothing due —
because a queue that lists only what it found looks complete when it is not.

Three more tools sit alongside it: **`next_action`** (the one action for a named
company, or the specific reason there isn't one), **`snooze_row`** and
**`schedule_reminder`**.

### Where per-row state lives

| State | Where | Note |
|---|---|---|
| The action itself | **nowhere** | recomputed on every run; a preview that wrote to the database would not be a preview |
| Snoozes | `snoozes` | keyed company + PoC |
| One-off reminders | `scheduled_reminders` | the only dates exempt from the weekend shift |
| Explicit activations | `row_activations` | see [Row activation](#row-activation--which-rows-the-bot-may-raise-unprompted) |
| Deadlines the bot announced | `deadlines` | unchanged; the reactive `set_deadline` tool still owns these |
| Digest carry-forward ages | `digest_items` | unchanged |
| Sheet-health dedup | `quality_flags` | unchanged |

### Checking it offline

```bash
python nextaction.py     # the state machine on fixtures: no sheet, no network, no database
```

39 assertions covering every trigger: that a closed row stops even when it has a
reply, that a blank closure does **not** stop it, that the override beats
everything, that the connection pair fires in the right order, that the
type-aware ask appears and disappears with the contact type, that 50% is the slow
lane, that a reply resets the silent-touch count, that a live snooze silences and
an expired one re-arms and reads as overdue, that no computed date lands on a
weekend and a scheduled one keeps its Saturday, and — the one that matters most —
that a row matching five triggers yields exactly **one** action.

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

**The full block above has no proactive outlet.** It was its own scheduled Friday
post, then a section of the daily digest — and the digest is retired. It is still
computed and still answered on demand.

**What does go out, if you opt in**, is the much shorter
[weekly funnel line](#the-weekly-funnel-line--opt-in): leading counts, then
lagging, one sentence, behind `WEEKLY_FUNNEL_ENABLED` (default **false**). The
conversion percentages, the stage funnel and the hygiene counts stay on-demand —
a block of numbers addressed to nobody is not a per-(type × owner) ask, and the
drip only carries those.

`WEEKLY_DIGEST_HOUR_IST` is retired, and so is the block it timed.

---

## The cadence — the phase-1 rules are retired

Phase 1 ran ten lettered rules (**a–j**) from Vaishnavi's *"Steps for Sales
Bot"* doc against the **outreach tracker** tab. Phase 2 moved the bot's sheet
world to the **"Outreach PoCs"** tab and retired both the tab and the rules.

**The rule evaluation is gone** — not disabled behind a flag, not left in place
returning nothing. Removed.

### What was removed

| Rule | What it did | Threshold, also removed |
|---|---|---|
| **a** `stale_followup` | last-followed-up date older than *n*, no response | `FOLLOWUP_STALE_DAYS` |
| **b** `intro_pending` | Connected = Y but the intro date is blank | — |
| **c** `start_interacting` | first contacted, never connected, *n* days on — **and its cold ceiling, `cold_summary` and the `cold list` tool** | `CONNECT_REMINDER_DAYS`, `CONNECT_REMINDER_MAX_DAYS` |
| **d** `alt_channel` | follow-ups ≥ *n* with no response → another channel | `ALT_CHANNEL_AT` |
| **e** `unresponsive` | follow-ups ≥ *n* with no response → mark them so | `UNRESPONSIVE_AT` |
| **f** `lock_meeting` | positive response, Next Steps blank | — |
| **g** `try_another_poc` | one alternative-PoC suggestion per rejected company | — |
| **h** `meeting_soon` | a meeting inside `MEETING_PREP_DAYS` | — |
| **i** `post_meeting` | meeting past, assets or next steps missing | — |
| **j** `nextstep_stall` | Next Steps unchanged for *n* days | `NEXTSTEP_STALL_DAYS` |

…plus two things that existed only to serve them:

- the **UPDATE-TRACKER fill-in asks** (`fill_in_gaps`) and their budget
  `UPDATE_TRACKER_MAX`;
- the **nightly master/tracker cross-check** (`crosscheck`),
  `CADENCE_CROSSCHECK_ENABLED` and `CADENCE_CROSSCHECK_MAX`.

**Every one of those environment variables is gone from `config.py`.** Setting
one now does nothing at all — a threshold left behind for a rule that no longer
exists is a lie in the config, and somebody would eventually tune it and wonder
why the digest never changed.

Two tools went with the rules that fed them: **`cadence_list`** (*"what did the
digest hold back"*) and **`cold_list`** (*"who never connected"*). A tool that
always returns an empty list with a confident description attached is worse than
no tool, because *"nothing"* reads as *"nothing is wrong"*. **`sheet_status`**
replaces them and answers what people were really asking through them.

The **meeting-prep briefs** are unwired for the same reason: they were selected
by rule (h). `prep.py` is unchanged and still builds a brief; what is gone is the
proactive trigger that chose which meeting got one. The `prep_briefs` SQLite
dedup is deliberately kept, so a phase-2 rule can re-wire it without re-briefing
every meeting already covered.

### What survives, and why

**The sheet-health flags.** They are a property of the *spreadsheet* rather than
of any cadence rule, they were never lettered, and they are the one thing here
that still has something true to say about a tab whose rules have not been
written yet.

**The plumbing** a phase-2 rule set will need: the rejection test
(`CADENCE_REJECTED_MARKERS`), the response vocabulary, the item shape, the
ranking, the two budgets (`URGENT_MAX`, `DIGEST_MAX_ITEMS`) and the owner
resolution (`SALES_DEFAULT_OWNER_ID`).

`cadence.evaluate_row()` still exists and still returns `[]`. It is the single
place a rule set plugs in, and it keeps its signature so that the day rules
return, one call site changes rather than five. **It is not a disabled rule set:
there is nothing behind it to enable.**

### What is left of it

The five cadence sections went with the digest format. What `cadence.py` still
computes is the **sheet-health flags**, and they have no proactive outlet — they
are logged and answerable, not announced.

That emptiness is deliberate and **visible**, never quiet:

```
[cadence] 12 ACTIVE row(s) considered (874 inactive row(s) never looked at — no
first-contact or connection date), 1 excluded as rejected; 0 item(s) and 1
sheet-health line(s); showing 0 + 1, 0 held. The phase-1 rules (a-j) are RETIRED,
so an empty item list is expected until a phase-2 rule set exists.
```

`quiet` and `broken` are different states and the log tells them apart.

> **No rule in `cadence.py` can make the bot speak.** There is no
> `guardrails.send` in that file and there must never be one. Every finding is
> handed to `_maybe_post_daily_digest`, which is still the only proactive send
> path in the codebase.

### Zero proactive paths remain wired to the old rules

Every proactive output path, and what feeds it now:

| Proactive path | Was | Is |
|---|---|---|
| Digest cadence sections | rules a–j on the tracker tab | sheet-health lines on the **Outreach PoCs** tab, **active rows only** |
| UPDATE-TRACKER asks | `fill_in_gaps` + `crosscheck` | removed; sheet-health only |
| Cold-cohort summary line | rule (c)'s ceiling | removed |
| HOT / STALLED / DEAD-DEAL flags | all tracker rows | **retired** — replaced by the [next-action state machine](#the-next-action-state-machine), which sends nothing |
| Next-action queue | — | **active rows only**, and it has **no proactive outlet**: `cadence preview` and the startup log |
| Weekly funnel numbers | all tracker rows | **active rows only** |
| Outreach-vs-plan check | all tracker rows | **active rows only** |
| Meeting layer's company list | all tracker rows | **active rows only** |
| Twice-weekly tracker reminder | counted rows missing follow-up cells | counts rows missing **both activation dates** |
| Meeting-prep briefs | rule (h) | **unwired** |
| Deadline announcements | unchanged | unchanged (SQLite-driven, not rule-driven) |

The single gate is `SalesBot._split_active`, which is the only place the
persisted activations are read — a path that forgot to read them would quietly
ignore an instruction somebody gave out loud.

### Who a line is addressed to

The row's own `owner` cell when the canonical tab has one (or `GTM_COLUMN_MAP`
names one: `{"outreach_pocs":{"owner":"Owned By"}}`), resolved against the
roster by display name; otherwise **`SALES_DEFAULT_OWNER_ID`**.

A ping needs **both** an id and roster membership — `guardrails.mention_for` is
the roster gate and it fails closed by naming the person in plain text instead.

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
good.

The row-level checks (misaligned rows, stray response values) run on **active
rows only** — a response value nobody standardised is worth reporting on a row
somebody is working, not on two hundred nobody has contacted. Broken formulas
are counted across the whole tab: a `#REF!` belongs to the tab rather than to a
row's readiness, and whoever fixes it needs the full count.

### Meeting-prep briefs — dormant

A brief is company + PoC, the mapped researcher row **with its caveats**, the
matching pitch from the positioning matrix, and anything on file from past
meeting notes — deduped in SQLite (`prep_briefs`) so it is written once per
meeting rather than every day until the meeting happens.

**Nothing triggers one today.** The brief was selected by phase-1 rule (h), which
is retired, so the MEETING PREP section of the digest is always empty. `prep.py`
is unchanged and still builds a brief; only the proactive trigger is gone.

The settings (`CADENCE_PREP_*`) and the SQLite dedup record are kept: throwing
that record away would mean re-briefing every meeting already covered the day a
phase-2 rule turns this back on.

### Seeing it before you trust it

At every startup the bot logs **what it is actually reading**, and sends nothing.
Four things fail silently on a live sheet, and all four are in this one report:

```
[sheet.world] CANONICAL TAB: 'Outreach PoCs'   (found by NAME, from GTM_POCS_TAB_TITLES)
[sheet.world]   rows: 886   columns: 23   header row: 1
[sheet.world]   discovered schema:
[sheet.world]       A  Sr. No.                -> sr_no             [RESTRICTED]
[sheet.world]       B  Company                -> company           [RESTRICTED]
[sheet.world]       J  First Contact Date     -> first_contacted
[sheet.world]       K  Connection Date        -> connected
[sheet.world]       S  Formula2                  (no role)         [RESTRICTED]
[sheet.world]
[sheet.world] WRITE LOCK
[sheet.world]   restricted (never written): 'A:I,S:X'  -> A:I, S:X
[sheet.world]   writable window between the bands: J:R
[sheet.world]   reading is UNRESTRICTED — this is a write lock only.
[sheet.world]   named columns inside the window (9):
[sheet.world]     J='First Contact Date' [first_contacted]
[sheet.world]     K='Connection Date' [connected]
[sheet.world]
[sheet.world] ROW ACTIVATION — a row is ACTIVE only with a first-contact or connection date
[sheet.world]   active rows considered:   12
[sheet.world]   inactive (never looked at): 874
[sheet.world]   active but rejected:      1
[sheet.world]   explicit activations held: 3
[sheet.world]
[sheet.world] CADENCE
[sheet.world]   The phase-1 rules (a-j), the cold ceiling, the fill-in asks and the
[sheet.world]   master cross-check are RETIRED.
```

1. **the canonical tab was renamed** → there is no tab at all;
2. **a column was renamed** → a role is unmapped in the schema dump;
3. **the activation columns are colour-coded or empty** → every row reads as
   inactive and the bot has nothing to talk about;
4. **the restricted bands have drifted** → the writable window points at a
   column somebody is using.

It replaced the phase-1 cadence dry run, which printed which rows each lettered
rule fired on. Those rules are retired, so that report would now be a page of
zeroes; these four numbers are what actually decides whether the bot can see
anything today.

The module's own checks run standalone, offline:

```bash
python cadence.py            # rejection, the retired rules, run(), the bands
```

It asserts that a rejected row is excluded, that `evaluate_row` fires **nothing**,
that `run()` reports the active / inactive / rejected counts separately, that a
stray response value is still flagged, and that the restricted-band check
classifies A, J and S correctly.

Everything also lands in `audit.jsonl` — a `sheet_world` record at startup
(carrying the tab, both row counts and the writable window), a `rows_activated`
record per explicit activation, and a `sheet_quality_flag` record per
sheet-health flag reported.

---

## The drip — a few short messages a day

**The one daily digest is retired.** One message at 10:00 carrying HOT /
DEADLINES / OVERDUE / ESCALATIONS / HYGIENE plus five cadence sections, a
tracker reminder, a to-do line and a funnel block, every item stamped
*"(3rd day)"* — that format is gone, and so is `SALES_DIGEST_MAX_PER_SECTION`.

**Why.** The digest existed because six kinds of scattered message got the bot
muted. It solved that and created the opposite problem: a wall of sections reads
like a report, gets skimmed, and asks a person to find their own name in it and
work out which three of forty lines are theirs.

The drip keeps the volume contract that made the digest worth having and spends
it differently:

> **One message per (action type × owner). Companies comma-separated, in one
> sentence. Never two types in a message. Never two owners.**

That rule is the whole design. A message with one subject and one owner is
answerable — *"yes, done"* means something. A document is not.

### The volume contract (plan §8)

| | |
|---|---|
| **At most** | `DAILY_MESSAGE_CAP` (3) proactive messages per **weekday** |
| **First at** | `SALES_DRIP_START` (10:00 IST) |
| **Then** | gaps of `MESSAGE_GAP_MINUTES` (90) ± `MESSAGE_JITTER_MINUTES` (15) |
| **Floor** | 90 − 15 = **75 minutes**, never less |
| **Overflow** | **rolls to tomorrow** — never dropped |
| **Empty queue** | **silence.** There is no "nothing to report" message |

**The positive-override exception to the roll.** Overflow normally waits. A group
in the override band does not: it is ranked first, so it takes a slot today by
construction — and a group that rolled yesterday and has since become an override
leads the next morning rather than queuing behind the same groups again. No
special case; just an ordering that re-evaluates daily.

**Replies are immediate.** The spacing is for *proactive* sends only. Someone
asking a question gets an answer straight away, and so does the ask-time deadline
announcement.

### The jitter is deterministic, and that is a correctness property

The offset for each slot is seeded on **(date, slot)** and nothing else — never
the clock, never the process. Every sweep tick recomputes the identical
schedule, which is what makes the restart guard work:

```
drip_sends  (on_date, slot) UNIQUE
```

One row per message that actually went out. A redeploy at 11:40 recomputes the
same times, sees slots 1 and 2 in SQLite, and resumes at slot 3. It replaces the
digest's single `sales_digest_date` marker, which only had to answer *"did
today's one message go out"*. `UNIQUE (on_date, slot)` also means two ticks
racing on a slot cannot both send: the loser's INSERT fails and it stops.

**Catch-up is paced too.** After a quiet morning — the kill switch off until
14:00, an outage — slots 1, 2 and 3 are all past due at once. The gap is measured
from the **last actual send**, not the planned time, so the backlog drains at the
drip's own pace instead of arriving as a burst. *(This was a real bug found by
the simulation below and fixed.)*

### One gentle re-ask, and only one

| State | What happens |
|---|---|
| never asked | **nudge** |
| asked < `DRIP_REASK_DAYS` (2) ago | **hold** — they have not had time |
| asked ≥ 2 days ago, never re-asked | **one re-ask**, softer, saying it is the last |
| already re-asked | clock resets — the next ask is an ordinary nudge |

The reset is what stops this becoming an escalation ladder. A subject that stays
undone for a month is asked about every few days in the same tone, not louder
each time. Two asks is a reminder; three is nagging — which is where this whole
design started.

### Suppress-or-convert — check before you nudge

**Before any proactive message sends**, the bot looks for evidence that the thing
it is about to ask for has already happened — in the synced meeting notes and in
what the team said in the sales channels.

**Evidence does not silence the nudge. It changes what the nudge is.**

| | |
|---|---|
| **task** | *"Vaishnavi — has the DM to Sahaj gone out?"* |
| **offer** | *"Looks like Sahaj Labs · Sahaj is already handled — meeting booked with Sahaj Friday 3pm (Vaishnavi in channel, Tue 08 Sep 2026). Want me to mark the meeting as booked on the row? Just say yes, or ignore me if I have got it wrong."* |

Suppressing would leave the row wrong **and** tell nobody, and a bot that goes
quiet is indistinguishable from one that has broken. The offer closes the loop
with one word back.

**"Yes" applies exactly what it showed you.** The proposed fields are stored on
the drip row when the offer goes out, so a bare affirmative has something
concrete to apply — and it goes through `sheetwrite.plan_writes` like any other
update, so the tiers, the bands, the fill rule and the ceiling all still hold. An
offer cannot reach a cell an ordinary reply could not. `is_affirmative` is
anchored at both ends: *"yes, but change the date to Friday"* does **not** match,
because that is a different instruction and belongs to the extractor.

#### Evidence is a ladder, not a lookup

| Stage | Means | Converts |
|---|---|---|
| 4 · quoted | quote / proposal / pricing sent | quote chases |
| 3 · meeting | meeting or call booked, invite sent | meeting proposals, and everything below |
| 2 · replied | they replied, came back, heard back | progress checks, and everything below |
| 1 · touched | DM sent, followed up, chased, emailed | DM checks, follow-ups, channel switches |

Evidence **at or above** a nudge's stage converts it. That is the case that
matters most: chasing a DM the morning after the meeting was booked is not
slightly wrong, it is the thing everyone in the channel can see is wrong.

`mark_unresponsive` and `pulse_check` are deliberately **not** convertible —
"they are unresponsive" is a judgement nobody records in passing, and a parked
deal has nothing that would count as already done.

#### Two things it took a real test to get right

**The phrases are past tense.** *"Will send"* and *"should book"* are the
opposite of evidence — they are the thing the nudge is about — and a matcher that
caught them would convert exactly the nudges that most need to go out as tasks.

**It matches on how people actually name an account.** The sheet says *"Sahaj
Labs"*; the channel says *"Sahaj"*. The first version matched only the full
company string and found **nothing** on a realistic message. It now builds an
identifier set — the company, its distinctive words (4+ characters, skipping
generic suffixes like *labs*, *inc*, *technologies*), and the PoC's name and
first name — and any of them counts.

#### Everything is quoted, and everything is logged

Every conversion names **what it found and where**: the meeting and its date, or
the author and theirs. A bot that says *"I think this is done"* without saying why
is asking to be trusted on a guess, and the first time it is wrong it will not be
trusted again.

Each one writes a `nudge_conversions` row (the sentence, the source, the
citation, the fields offered) and a `nudge_converted` line in `audit.jsonl`. A
conversion is a judgement, and a wrong one is otherwise invisible — the symptom
is a nudge that arrived as a strange offer instead of a question.

Only ever looks back **`NOTES_LOOKBACK_DAYS` (7)**. A note from three weeks ago
saying *"we'll book something"* is not evidence that a meeting exists now.

The check runs **immediately before the send**, not at plan time: the queue is
planned hours ahead, and evidence has to be as fresh as the message.

### Research briefs — mention-triggered, and copy material

> *"brief me on Sahaj (Sahaj Labs)"*

Five things, in order: **who they are** · **how much the role weighs** ·
**what of their work maps to our lanes** · **the angle** · **a draft message**.

**It is never sent and never written to the sheet.** The bot has no outbound
channel to a prospect and this does not give it one — it hands a human a draft to
edit. There is no scheduled brief and no brief attached to a nudge; somebody
asks, or nothing happens.

**Role weight is a judgement made explicit.** Someone who has led a lab since its
inception can say yes; a recent MTS has to ask, and the outreach should be written
to be forwarded upward. The brief states which it thinks it is looking at **and
why, in one sentence**, so a wrong read is arguable rather than buried.

**The lanes come from the team's own documents** — `sales_policy.md` and the
strategy doc, read at brief time. Re-writing the positioning changes the briefs
with no code change.

#### What it may fetch

1. **Only URLs already on that person's row.** No web search, no following links
   out of a fetched page, no guessing a URL from a name. No links on the row and
   the brief says so and stops.
2. **Only `RESEARCH_ALLOWED_DOMAINS`** (default `arxiv.org`). Matched on the
   registered domain and its subdomains — `export.arxiv.org` passes,
   `notarxiv.org` does not. Anything else is **refused with a one-line note
   naming the domain**, repeated verbatim in the brief:

   ```
   I did not fetch: https://linkedin.com/in/x — linkedin.com is not in
   RESEARCH_ALLOWED_DOMAINS (arxiv.org).
   ```

   Named rather than skipped, because nobody should read a partial brief as a
   complete one.
3. **Bounded** — `RESEARCH_FETCH_TIMEOUT_SECONDS`, `RESEARCH_FETCH_MAX_BYTES`,
   `RESEARCH_MAX_LINKS`. A slow or enormous page degrades the brief instead of
   hanging an answer somebody is waiting on. The allow-list is re-checked inside
   `fetch()` itself, not trusted from the caller.

#### LinkedIn is API-or-nothing

With `LINKEDIN_API_*` credentials the LinkedIn half runs. Without them — the
current state — the brief says so, **in those words**:

> LinkedIn access is pending — no API credentials are configured. I have NOT
> scraped anything to fill the gap and will not: their terms forbid it. So the
> role and tenure below come from the sheet alone, and may be out of date.

There is no scraping fallback and there must not be one. A career history quietly
missing from a brief reads as *"this person has no notable history"*, which is a
different and wrong claim.

### Events & summits

The playbook's **Events & Summits** tab. Each event earns **one** reminder, at
**`EVENT_LEAD_DAYS` (20)** out, and never another.

**Once, forever.** The dedup is a permanent `event_reminders` row, not a per-day
marker: a conference the team has already decided about does not need reminding
twice, and *"we mentioned it in March"* is not a reason to mention it again in
April. The key is the event's **name and its date**, so an event that **moves**
earns a fresh reminder — the new date is new information — while re-reading the
same row tomorrow does not.

**T-20 is a window, not an exact day** (the event falls between today and T+20).
The drip can only speak when a slot is free, and an exact-day test would drop a
once-forever reminder entirely on a busy Tuesday.

**The reminder is recorded only once the message has landed.** Recorded before
the send, a refused message would burn an event's single reminder forever — and
*forever* is not a word to be careless with. The lookup **fails closed**: if the
table cannot be read the event is treated as already reminded, because a
duplicate is the exact thing it exists to prevent.

**A past event is never reminded about.** Obvious, and worth stating: the sheet
keeps last year's conferences.

It **rides the drip** — grouped, ranked, spaced and counted against
`DAILY_MESSAGE_CAP`, with the kill switch applying. The event's name goes in the
company slot, so a message about three summits reads exactly like one about three
accounts.

### The weekly funnel line — opt-in

One short Friday message: leading counts, then lagging. Nothing else.

```
This week: 12 outreach, 3 replies, 2 meetings booked and 8 follow-ups.
Landed: 1 pilots, 0 paid and 0 repeats. Nothing needed from you — just so it is
written down somewhere.
```

**Counts only.** No commentary, no trend, no *"up 12% on last week"* — the bot
does not have enough weeks of clean data to say anything about a trend, and a
confident sentence about noise is worse than a number. **Zeros stay in**:
dropping them would turn a bad week into a short sentence and a good week into a
long one, which is exactly the quiet editorialising a counts-only line exists to
avoid.

**`WEEKLY_FUNNEL_ENABLED` defaults to false**, per the plan. A weekly number
nobody asked for gets skimmed, and it spends one of the day's three slots. It is
ranked **last** in the queue, so it can never take a slot from a reply somebody is
waiting on.

### The kill switch: `SALES_DIGEST_ENABLED` — unchanged

**Same name, same semantics, same log line.** The drip *inherits* the switch; it
does not get one of its own.

That is deliberate. The switch is currently `false` on the server, and an
operator who set it that way to stop the bot talking must not discover that a
rewrite quietly re-armed it under a new variable.

```
[digest] suppressed — SALES_DIGEST_ENABLED=false
```

One line per suppressed day, in the words it always used. Read **live** at send
time, so flipping it takes effect on the next sweep tick without a restart, and
checked after the cheap gates and **before** anything is composed or sent — so a
suppressed day makes no model call and writes no slot row. Nothing is written
while it is off, so there is no state to unwind to end the silence and no backlog
to replay when it comes back.

`false` still leaves everything computing: deadlines tracked, the next-action
queue evaluated, SQLite and `audit.jsonl` written, `cadence preview` and
`sheet status` answered on demand.

### The voice

Every proactive message — and every event reminder — is written as a **warm
sales head** who has already looked at the sheet and is mentioning one thing on
the way past. **One thought per message. Always an out.** Thanks where earned.
No headers, no bullets, no labels, no stacked imperatives, no emojis.

The rules live in `persona.PROACTIVE_VOICE`; the **ten voice exemplars** live in
`sales_policy.md` under `### Voice exemplars` and are read **fresh on every
message**. Rewriting the bot's proactive voice is a markdown edit — no restart,
no deploy.

> The ten exemplars are written to the Cadence Plan v2 §4 style description. If
> the plan's own samples differ in wording, paste them over the list in
> `sales_policy.md` and they are live immediately.

**The model's output is checked, not trusted.** A composed message containing a
bulleted list, a header, an emoji or more than two paragraphs is rejected and the
deterministic template goes out instead — shipping it would teach the team that
the voice rules are decorative. The template is still one sentence, still names
every company, and still ends with an out, so a model outage costs polish and
never the message.

### Seeing a day before it goes out

```bash
python -m main --dry-run-drip               # today
python -m main --dry-run-drip 2026-09-12    # a Saturday, to prove it stays quiet
python drip.py                              # the contract, on fixtures, offline
```

`--dry-run-digest` still works as an alias. `python drip.py` runs 28 assertions:
the grouping rule, the cap, the 75-minute floor, determinism across two plans, a
restart resuming at slot 2, weekend silence, all five states of the re-ask clock,
and that the fallback text has no bullets, names its owner once and gives an out.

### A seeded day, checked against §8

7 due items · 2 owners · 3 types:

```
GROUPS (one message per type × owner)
  P0  meeting_proposal  Vaishnavi  Acme and Borealis
  P1  quote_chase       Kushal     Delta
  P1  quote_chase       Vaishnavi  Cinder
  P2  followup          Kushal     Echo, Fathom and Gantry

PLANNED
  10:00  slot 1  meeting_proposal -> Vaishnavi
    Vaishnavi — Acme and Borealis came back to us and there is no meeting on the
    books yet. Worth proposing a time while it is warm — no rush if you are
    mid-something, just tell me when you have.
  11:24  slot 2  quote_chase -> Kushal
  12:48  slot 3  quote_chase -> Vaishnavi
  [rolls to tomorrow] followup × Kushal — Echo, Fathom and Gantry

CONTRACT   messages 3/3 · gaps [84, 84] min (floor 75) · mixed types 0 ·
           mixed owners 0 · 1 rolled          -> HONOURED
```

### What went with the format

The digest was the only outlet these had, and they lost it:

| Section | State now |
|---|---|
| HOT / HYGIENE | already retired with the row-hygiene flags |
| DEADLINES / OVERDUE / ESCALATIONS | **still tracked in SQLite, no longer announced.** The drip groups the next-action queue, and a chase is not an action type on it |
| Tracker reminder (Mon/Fri) | retired with the format |
| To-do sheet line | retired; the sheet is still created, shared and answered on demand |
| Weekly funnel block | retired; the numbers are still computed and answered on demand |
| Outreach-vs-plan check | retired; still answered on demand |
| Sheet-health flags | still computed; no proactive outlet |
| Carry-forward "(3rd day)" ages | removed. `digest_items` is kept, unread, so the history is not thrown away |

**Re-surfacing any of them means deciding who the message is *for*** — the drip's
contract is one type, one owner, one ask — which is a product question rather
than a config one.

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
| `main.py` | entry point; validates env, logs scope and source statuses, connects. `--dry-run-drip [YYYY-MM-DD]` prints a day's whole plan and sends nothing (`--dry-run-digest` is an alias) |
| `bot.py` | the Discord client: routing, question answering, chasing, and the one daily digest — its only proactive send |
| `digest.py` | **the digest format is retired.** What is left is the clock: `parse_time` and `is_due` |
| `evidence.py` | **suppress-or-convert**: the staged evidence ladder, the identifier matching, and the record-offer text. Almost pure — only `gather` does I/O |
| `events.py` | **events & summits and the weekly funnel line**: the T-minus window, the permanent dedup key, and the counts-only Friday message. Pure — rows in, action dicts out |
| `research.py` | **research briefs**: link extraction, the domain allow-list, the bounded fetch, the LinkedIn status, and the brief prompt. Copy material only — never sent, never written |
| `sheetwrite.py` | **what the bot may write, and when**: the three tiers, the fill rule, the contact-detail ask, the terminal-word gate, the cell ceiling, the echo line, and the snooze/reminder parsing. Pure — plans in, plans out, no I/O |
| `drip.py` | **the drip scheduler**: grouping by (type × owner), the deterministic schedule, the re-ask clock, the fallback message text, the preview and the volume-contract report. Pure — it sends nothing |
| `cadence.py` | **the phase-1 rules (a–j) are removed from here** — what is left is exclusion, the sheet-health flags, priority, the two caps, and the boot report. `evaluate_row()` returns `[]` and is the single place a phase-2 rule set plugs in. Pure: rows in, dicts out — no sheet reads, no writes, no Discord |
| `activation.py` | **the activation rule**: a row is active only with a first-contact or connection date; the mention-request matcher; the split every proactive path goes through. Pure — the persisted activations are passed in |
| `nextaction.py` | **the next-action state machine**: eleven triggers in a fixed order, exactly ONE action per active row, the priority bands, the snooze re-arm, the weekend shift, and the `cadence preview` renderer. Pure: rows in, dicts out — no sheet reads, no writes, no Discord, **no send path** |
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
| `tracker.py` | the canonical tab read as a pipeline: last touch, open/engaged, the twice-weekly reminder, the playbook's six-stage funnel definition, and the week's leading/lagging metrics. **The three flags are removed** — see `nextaction.py` |
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
