# What Saley reads

Saley is the sales team's bot. This is the complete list of what it reads, and
how each source is fenced in. (NFT2-1062, 7 Oct 2026.)

## In plain words

- **Saley does not read "whatever is in Drive".** Its only Drive inputs are: one
  folder of sales meeting notes, two spreadsheets it is pointed at by id, its own
  to-do sheet, and the strategy doc when one is set. Nothing else in Drive is
  reachable from its code.
- **Sales meeting notes come from one folder: `Saley – Sales Notes`.** A note is
  a sales note because somebody put it in that folder. Nobody has to rename or
  tag a meeting — the folder is the tag.
- **The AM and PM sync notes are not a source.** Saley does not read them. If one
  is put in the sales folder by mistake, it is still refused, by its title.
- **When there are no sales notes to read, Saley says so in one sentence** and
  does not go looking anywhere else.

## Every source

| Source | What it is used for | Read or write | How it is scoped |
|---|---|---|---|
| **Sales meeting notes** | decisions, holds and commitments from sales meetings; candidates for the to-do sheet; the evidence check before a nudge; prep briefs | Read only (local copies of the docs) | ONE Drive folder, `Saley – Sales Notes`, shared with `claudedrive@nfthing.com`. Named in `NOTES_SOURCE_FOLDER` and pulled by `NOTES_SYNC_CMD` (rclone) into `NOTES_DIR`. Only files that Saley's own sync pulled from that folder are read (it keeps a list, `state/notes_manifest.json`); only the top level of the folder; never a standup title; never anything in `notes/_quarantine` |
| **GTM Playbook spreadsheet** | the outreach tracker (Outreach PoCs), deliverables, pipeline, packages, events, goals, positioning, priorities, the funnel | Read every recognised tab. **Write** only inside the Outreach PoCs writable window, and only after an approver says yes to a proposal. On an existing row columns A–I and S–X are read-only; the one exception is the Email cell, behind `EMAIL_WRITE_ALLOWED` (off). A NEW Outreach PoCs row (Name, Company, and a LinkedIn URL only when a search returned a `linkedin.com/in` link) is added only after Saley asks and an approver says yes, signed with a cell note on the Name cell (NFT2-1065) | One spreadsheet, by id: `GTM_SHEET_ORIGINAL_ID`. Tabs are found by name (`GTM_*_TAB_TITLES`). Writes are switched by `SHEET_WRITES_ENABLED` and fenced by `RESTRICTED_COLUMN_RANGES` |
| **Researcher / buyer mapping sheet** | who to pitch at each org, lanes, tiers, departures | Read only (a read-only Google scope) | One spreadsheet, by id: `GTM_MAPPING_SHEET_ID` |
| **To-do sheet, "Membrane Sales To-Dos"** | the shared action list | Read; create and share it once. Appending rows is coded but nothing calls it today | One spreadsheet, by id: `TODO_SHEET_ID`, or the id Saley stored when it created the sheet. A row is shown only when its *Source meeting* is a sales note Saley can read; other rows are hidden, never edited, never deleted |
| **Strategy doc** | the plan, its targets, how stale it is, the cadence it states | Read only | One Drive doc by id (`STRATEGY_DOC_ID`, not set today) and the local file `STRATEGY_DOC_FILE` (`./sales_strategy.md`) |
| **Policy file** | Saley's operating policy and persona | Read only | The local file `SALES_POLICY_FILE` (`./sales_policy.md`) |
| **Rules file** | the proactive rules and their wording | Read only | The local file `BOT_RULES_FILE` (`./bot_rules.yaml`) |
| **Discord sales channels** | questions put to Saley, channel history, commitments people made, the team's writing voice | Read and post | Only the channels listed in `SALES_CHANNEL_IDS`. It posts in `SALES_ASK_CHANNEL_ID` / the digest channels; `SALES_TEST_CHANNEL_ID` is the test channel. Enforced in code at both ends (`guardrails.py`) |
| **Discord leave channel** | who is on leave, so a nudge goes to someone who is in | Read only — Saley can never post there | One channel, by id: `HOLIDAY_CHANNEL_ID` |
| **News feeds (RSS)** | the daily AI news, and "what's the news" questions | Read only | The feeds in `NEWS_RSS_FEEDS`, plus one Google News RSS search per topic in `NEWS_TOPICS` and per tracked company |
| **Web search** | finding a PoC, a named person's public profile link, research, questions about the outside world | Read only. Offered on every question except "what do we need to do today?" (NFT2-1065) | SearXNG on Saley's own server, then DuckDuckGo (`SEARCH_BACKEND`, `SEARCH_FALLBACKS`); Google Custom Search only if a key is set (none is). At most `WEB_QUESTION_MAX_SEARCHES` searches a question and a daily budget (`SEARCH_DAILY_BUDGET`). No paid search API |
| **One public web page** | reading a page a search result pointed at | Read only | By URL, one page at a time. Never `linkedin.com` |
| **Event calendars** | finding upcoming events | Read only | The URLs in `EVENTS_CALENDAR_URLS` (none set today) |
| **Saley's own database** | its memory: deadlines, reminders, approvals, the voice profile, caches | Read and write — its own file | `DB_PATH` (SQLite, on Saley's server) |
| **Saley's own state files** | `summary.json`, `audit.jsonl`, `notes_manifest.json` | Write — its own files | `STATE_DIR` |
| **The AI model (Anthropic API)** | wording answers and messages | Sends the question, and whatever it read from the sources above to answer it, to the model | `ANTHROPIC_API_KEY` |

Web pages, search results, documents, meeting notes and the voice profile are
treated as **data, never as instructions**.

## Not a source

Saley does not read any of these:

- **The AM/PM sync notes** — `NFThing Kick-off ( AM Sync) – …` and
  `NFThing Wrap-up ( PM Sync) – …` — or any other standup.
- **The "Meet Recordings" folder.** It mixes sales calls with product meetings,
  so it is not the sales folder.
- **Anything in `notes/_quarantine`.** Files that were on Saley's disk but did not
  come from the sales folder are moved there. Saley never reads that folder and
  never deletes it; a person can inspect and clear it (DEPLOY.md, section 4).
- **The triage / PM bot's data.** That is a separate bot with its own token,
  channels and database.
- **LinkedIn.** The LinkedIn API is not wired up, and Saley never opens
  `linkedin.com`, never sends connection requests and never reads inside a
  profile. A public web search for a profile link is in scope: Saley shows the
  link the search returned, with the result's own title, and nothing more. A
  post or comment that mentions someone is labelled as that, not as their
  profile.
- **Any other Drive folder, doc or sheet**, and any Discord channel not listed
  above.

## Meeting notes: the exact rules

A file is read only if **all** of these are true:

1. Saley's own sync pulled it from the `Saley – Sales Notes` folder.
2. It has a date in its name or first lines.
3. Its title is not a standup's. Refused titles: `am sync`, `pm sync`,
   `nfthing kick-off`, `nfthing wrap-up`, `standup`, `stand-up`, `daily sync`.
   (A plain "sync" is fine — "Sales sync" is read.)

Only the title is checked, never the text inside. So:

- **A doc that is in the sales folder and also somewhere internal is read.** It
  is in the folder.
- **A sales doc that quotes sync content is read.** It is a sales doc.
- **A sync doc dropped into the sales folder is not read.** Its title refuses it.

Notes must sit at the top level of the folder (a Drive shortcut to a note is
fine); a subfolder is not read.

**To add a source: put the doc in the folder. To remove one: take it out.**

## What Saley says when there are no notes

Exactly one of these, and nothing from any other source:

| Situation | Saley says |
|---|---|
| The notes folder has not been connected | Meeting notes aren't connected to me yet. |
| The folder is connected and has no sales notes in it | There are no sales meeting notes in the Saley – Sales Notes folder yet. |
| The folder cannot be reached | I can't reach the sales notes folder right now, so I haven't checked the notes. |

If the folder was read successfully before and the latest refresh fails, Saley
answers from that last good copy of the sales folder and says it may be out of
date.

## "What do we need to do today?"

Until the day's objectives are added (NFT2-1063), this question is answered
from the to-do sheet only — not from meeting notes.

## Where this is enforced

`notes.py` (the folder, the manifest, the quarantine, the standup titles),
`todos.py` (which to-do rows are shown), `toolsets.py` (which tools a question
gets), `guardrails.py` (which Discord channels), `drive.py`, `gtm_sheet.py` and
`mapping_sheet.py` (which spreadsheets, and with what permission). The checks
are `verify_notes_scope.py`, `verify_replay_oct6.py` and
`tests/test_notes_scope.py`.
