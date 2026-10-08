"""SQLite-backed state for the sales bot (config.DB_PATH, default sales_bot.db).

A FRESH database, not the PM bot's. There is no approval queue here, no ticket
mapping and no classification record, because this bot files nothing — its only
output is a Discord message, and since the digest consolidation, exactly ONE
unprompted one a day. Five tables:

1. `chases` — the deadline chasing this bot exists to do. Someone said in a sales
   channel "I'll send Acme the deck tomorrow"; that is a promise with an implied
   due time and nobody tracking it. A row records who owes what, by when, how
   many times we've asked, and how it ended. The ONLY effect a row can ever have
   is a reminder posted in the channel the promise was made in.

2. `nudges` — THE RATE LIMIT, and the reason chasing is capped from day one. The
   bot posts unprompted @-mentions on the chase path; what stops that becoming a
   nag is that every nudge must check this log before it goes out and record
   itself after. `(target_key, subject_key, kind)` identifies "this person, about
   this promise, for this reason" and is never repeated inside
   COS_NUDGE_WINDOW_HOURS. See `was_nudged_since` / `record_nudge`.

3. `digest_items` — CARRY-FORWARD for the one daily digest. Whatever is still
   outstanding reappears tomorrow with its age ("3rd day"); this table is where
   that age lives, so a redeploy doesn't reset every item to day one.

4. `meta` — the bot's own bookkeeping (the date the daily digest last went out,
   the date the state summary was last written), so a restart doesn't redo daily
   work. The digest's once-a-day guarantee is exactly this: `sales_digest_date`
   is written after the digest posts, and a redeploy an hour later reads it back
   and stays quiet.

All timestamps are UTC 'YYYY-MM-DD HH:MM:SS' strings — the same shape SQLite's
CURRENT_TIMESTAMP writes — so they sort and compare correctly as plain strings.
"""
import json
import logging
import re
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)


def _hours_between(earlier_iso: str, later_iso: str) -> Optional[float]:
    """Hours between two ISO timestamps, or None when either cannot be read.

    None rather than 0 on an unreadable value, and every caller treats None as
    "outside the window". An undo window that failed OPEN would let a write from
    last week be reverted by somebody who thought they were undoing this
    morning's.
    """
    from datetime import datetime as _dt

    try:
        a = _dt.fromisoformat(str(earlier_iso))
        b = _dt.fromisoformat(str(later_iso))
    except (TypeError, ValueError):
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        # One naive, one aware. A naive value is read as IST — the convention
        # every writer here follows — rather than stripping the other side's
        # zone, which would compare a UTC value as though it were IST.
        import deadlines as _dl
        a = a if a.tzinfo else a.replace(tzinfo=_dl.IST)
        b = b if b.tzinfo else b.replace(tzinfo=_dl.IST)
    return (b - a).total_seconds() / 3600.0


def _days_between(earlier: str, later: str) -> int:
    """Calendar days between two 'YYYY-MM-DD' strings, or 0 when either is
    unreadable. Only used by the digest carry-forward gap rule, where treating a
    malformed date as "no gap" keeps an item's age rather than resetting it."""
    from datetime import date as _date

    try:
        a = _date.fromisoformat(str(earlier)[:10])
        b = _date.fromisoformat(str(later)[:10])
    except (TypeError, ValueError):
        return 0
    return (b - a).days


def _norm_key(text: str) -> str:
    """A company name as a comparable key: lower-cased, punctuation collapsed.

    "Acme Corp", "acme corp" and "Acme  Corp." are one company. Without this a
    case change in the sheet would read as a brand-new company and R11 would
    announce one that has been there for months.
    """
    v = re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower())
    return re.sub(r"\s+", " ", v).strip()


def _iso_days_ago(today_iso: str, days: int) -> str:
    """`days` before an ISO date, as an ISO date. Falls back to the input when
    it cannot be parsed, which widens the window rather than narrowing it — a
    rule that looks too far back is noisy, one that looks too far forward is
    silent, and noisy is the recoverable direction."""
    from datetime import date as _date, timedelta as _td
    try:
        y, m, d = (int(p) for p in str(today_iso)[:10].split("-"))
        return (_date(y, m, d) - _td(days=max(0, int(days)))).isoformat()
    except Exception:
        return str(today_iso)[:10]


SCHEMA = """
-- One row per commitment the bot is waiting on. `due_at` is when we may FIRST
-- nudge, derived from what the person said ("tomorrow" → +1 day).
-- status: open   — still waiting
--         closed — they came back (replied, or someone ✅'d the nudge)
--         stale  — chased COS_NUDGE_MAX_ATTEMPTS times with no answer; we stop.
--                  Stopping is deliberate: a bot that keeps asking forever is a
--                  bot people mute.
CREATE TABLE IF NOT EXISTS chases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT NOT NULL,
    message_id          TEXT NOT NULL UNIQUE,
    jump_url            TEXT,
    person_id           TEXT,
    person_name         TEXT NOT NULL,
    what                TEXT NOT NULL,
    promised_at         TIMESTAMP NOT NULL,
    due_at              TIMESTAMP NOT NULL,
    status              TEXT NOT NULL,
    reminders_sent      INTEGER NOT NULL DEFAULT 0,
    last_reminder_id    TEXT,
    last_reminded_at    TIMESTAMP,
    flagged             INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chase_due ON chases(status, due_at);
CREATE INDEX IF NOT EXISTS ix_chase_reminder ON chases(last_reminder_id);

-- One row per nudge the bot has POSTED. This table IS the rate limit; see the
-- module docstring. `target_key` identifies the audience ("person:<id_or_name>")
-- and `subject_key` the thing being chased ("chase:<id>").
CREATE TABLE IF NOT EXISTS nudges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key    TEXT NOT NULL,
    subject_key   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    channel_id    TEXT,
    message_id    TEXT,
    sent_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nudges_key
    ON nudges(target_key, subject_key, kind, sent_at);

-- DEADLINES — authoritative. A deadline EXISTS once it is here; the sheet's
-- "Next Deadline (bot)" column is a mirror that may fail to be written without
-- the deadline being lost.
--
-- `source` is the conflict rule made explicit:
--   'bot'   — the bot derived this date from the cadence.
--   'human' — a person typed a date into the ORIGINAL sheet. Adopted as-is and
--             NEVER overwritten; a human date always wins.
-- `due_date` is a calendar date in IST (YYYY-MM-DD), not a timestamp: the team
-- works to days, not to minutes.
-- (company, kind) is unique, so one company has one live deadline per kind
-- rather than an accumulating pile of near-duplicates.
CREATE TABLE IF NOT EXISTS deadlines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    company_key     TEXT NOT NULL,   -- normalised company, for lookup
    kind            TEXT NOT NULL,   -- outreach_followup | reply_chase | meeting_prep
    due_date        TEXT NOT NULL,   -- YYYY-MM-DD, IST
    rule            TEXT NOT NULL,   -- the human-readable rule that produced it
    source          TEXT NOT NULL,   -- bot | human
    sheet_row       INTEGER,         -- 1-based tracker row, for the cell write
    sheet_target    TEXT,            -- copy | original | off — where it was mirrored
    sheet_cell      TEXT,            -- e.g. "S14"; empty when the write didn't happen
    owner_id        TEXT,            -- Discord id of whoever is chased about it
    owner_name      TEXT,
    channel_id      TEXT,            -- where it was announced
    announced       INTEGER NOT NULL DEFAULT 0,
    reminded        INTEGER NOT NULL DEFAULT 0,   -- the one-day-before reminder
    chases_sent     INTEGER NOT NULL DEFAULT 0,   -- overdue chases so far
    escalated       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open', -- open | met | superseded | cancelled
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_key, kind)
);
CREATE INDEX IF NOT EXISTS ix_deadline_due ON deadlines(status, due_date);
CREATE INDEX IF NOT EXISTS ix_deadline_company ON deadlines(company_key);

-- Row-hygiene flags already raised. RETIRED as a rate limit: hygiene flags no
-- longer post on their own at all, they are a section of the ONE daily digest,
-- and `digest_items` below is what makes an item appear once a day and carry an
-- honest age. The table and its two methods are kept because they are the
-- historical record of what was flagged before the digest existed.
CREATE TABLE IF NOT EXISTS flags_sent (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_kind     TEXT NOT NULL,   -- hot | stalled | dead_deal
    company_key   TEXT NOT NULL,
    on_date       TEXT NOT NULL,   -- YYYY-MM-DD IST; the once-a-day key
    channel_id    TEXT,
    message_id    TEXT,
    sent_at       TIMESTAMP NOT NULL,
    UNIQUE (flag_kind, company_key, on_date)
);
CREATE INDEX IF NOT EXISTS ix_flags_date ON flags_sent(on_date, flag_kind);

-- CARRY-FORWARD for the ONE daily digest. One row per distinct digest item
-- (a chase, a deadline, a flagged sheet row), recording the day it first showed
-- up and how many digests it has survived. That count is the "(3rd day)" marker
-- on the item's line.
--
-- IT LIVES IN SQLITE RATHER THAN IN MEMORY ON PURPOSE. A redeploy at 09:55
-- must not reset every age to day one — an item's age is the single most useful
-- thing on its line, and one that silently restarts at "1st day" after every
-- push is worse than no age at all.
--
-- An item that gets resolved simply stops being collected, so its row stops
-- being touched and is pruned later. There is no "resolved" state and no
-- resolved message: the digest is what's outstanding, nothing else.
CREATE TABLE IF NOT EXISTS digest_items (
    item_key    TEXT PRIMARY KEY,   -- e.g. "chase:14", "hot:openai:37"
    section     TEXT NOT NULL,      -- hot | deadlines | overdue | escalations | hygiene
    first_seen  TEXT NOT NULL,      -- YYYY-MM-DD IST
    last_seen   TEXT NOT NULL,      -- YYYY-MM-DD IST
    times_seen  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_digest_items_seen ON digest_items(last_seen);

-- WHAT THE NEXT STEPS CELL SAID, AND SINCE WHEN.
--
-- Cadence rule (i) is "Next Steps present but UNCHANGED for n days", and
-- "unchanged" is not a property of a spreadsheet cell — the sheet has no
-- history the bot can read. So the bot keeps its own: one row per tracked
-- sheet row, holding a hash of the current Next Steps text and the date that
-- text was FIRST seen. Days-unchanged is (today - first_seen).
--
-- The text itself is stored only as a short sample, for the log line that
-- explains why a row fired. The hash is what the comparison uses, so a 400-char
-- next-step costs the same as a short one.
CREATE TABLE IF NOT EXISTS nextstep_state (
    row_key     TEXT PRIMARY KEY,   -- "<company>|<poc>" from the tracker tab
    text_hash   TEXT NOT NULL,      -- sha1 of the normalised Next Steps text
    text_sample TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,      -- YYYY-MM-DD IST — when THIS text appeared
    last_seen   TEXT NOT NULL       -- YYYY-MM-DD IST — last time it was observed
);
CREATE INDEX IF NOT EXISTS ix_nextstep_seen ON nextstep_state(last_seen);

-- MEETING-PREP BRIEFS ALREADY WRITTEN.
--
-- A brief is attached to the digest ONCE PER MEETING, not once a day until the
-- meeting happens. The key is the company + PoC + meeting date, so moving a
-- meeting to a new date legitimately earns a fresh brief and re-reading the
-- same sheet row tomorrow does not.
CREATE TABLE IF NOT EXISTS prep_briefs (
    meeting_key  TEXT PRIMARY KEY,  -- "<company>|<poc>|<YYYY-MM-DD>"
    company      TEXT NOT NULL DEFAULT '',
    poc          TEXT NOT NULL DEFAULT '',
    meeting_date TEXT NOT NULL DEFAULT '',
    sent_on      TEXT NOT NULL,     -- YYYY-MM-DD IST the digest carried it
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_prep_briefs_sent ON prep_briefs(sent_on);

-- SHEET-HEALTH FLAGS ALREADY REPORTED, AND WHAT THEY SAID.
--
-- The three data-quality findings — a broken formula, a misaligned master row,
-- a response value nobody standardised — are reported ONCE and then not again
-- UNTIL THEY CHANGE. A daily reminder about a #REF! everybody already knows
-- about is exactly the drip that gets a digest muted, and "we told you
-- yesterday" is not new information.
--
-- The SIGNATURE is what makes "until fixed" work without the bot having to know
-- what fixed looks like: it is a short description of what was actually found
-- (which columns, how many cells, which values). A flag whose signature matches
-- the stored one is skipped. Fix half of it and the signature changes, so the
-- remaining half is reported again — which is right, because the finding is now
-- a different finding.
CREATE TABLE IF NOT EXISTS quality_flags (
    flag_key    TEXT PRIMARY KEY,   -- e.g. "broken_formula:Master Pipeline"
    signature   TEXT NOT NULL,      -- what was found, last time it was reported
    first_seen  TEXT NOT NULL,      -- YYYY-MM-DD IST
    last_seen   TEXT NOT NULL,      -- YYYY-MM-DD IST
    times_seen  INTEGER NOT NULL DEFAULT 1
);

-- EXPLICITLY ACTIVATED ROWS.
--
-- A row on the canonical "Outreach PoCs" tab is normally ACTIVE only when it
-- carries a first-contact date or a connection date; an inactive row is
-- invisible to every proactive feature. The one exception is somebody asking
-- for it by name ("set connection reminders for the others at Acme"), and this
-- table is that exception made durable.
--
-- IT IS KEYED ON COMPANY+PoC, NEVER ON THE SHEET ROW NUMBER, because row
-- numbers move the moment anybody sorts the tab and an activation that followed
-- a number would silently transfer to whoever landed in that row next.
--
-- It persists because the alternative is a bot that forgets, on the next
-- restart, an instruction it was given out loud — and the person who gave it
-- would have no way of knowing.
CREATE TABLE IF NOT EXISTS row_activations (
    row_key      TEXT PRIMARY KEY,   -- normalised "company|poc"
    company      TEXT NOT NULL,      -- as displayed, for the answer text
    poc          TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,      -- the request, in the asker's words
    requested_by TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL       -- YYYY-MM-DD IST
);

-- SNOOZES — "follow up in 5 days", "come back to this on the 20th".
--
-- A snoozed row emits NOTHING from the next-action state machine until its
-- date. On and after that date the row's action comes back with its due_date
-- RE-ARMED TO THE SNOOZE DATE rather than to whatever the trigger would have
-- computed — because the person who said "follow up on the 20th" said WHEN, and
-- a bot recomputing a different date would be overruling them.
--
-- A snooze whose date has passed is therefore OVERDUE, and stays overdue and
-- visible until somebody acts on it. It is not silently dropped: an instruction
-- that expires into nothing is an instruction the bot took and then ignored.
--
-- Keyed on company+PoC like every other per-row record here, never on the sheet
-- row number, which moves the moment anybody sorts the tab.
CREATE TABLE IF NOT EXISTS snoozes (
    row_key      TEXT PRIMARY KEY,   -- normalised "company|poc"
    company      TEXT NOT NULL,
    poc          TEXT NOT NULL DEFAULT '',
    until_date   TEXT NOT NULL,      -- YYYY-MM-DD IST
    note         TEXT NOT NULL DEFAULT '',   -- the request, in the asker's words
    requested_by TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snoozes_until ON snoozes(until_date);

-- EXPLICITLY SCHEDULED REMINDERS — one-offs somebody asked for at a specific
-- time ("remind me about Acme on Saturday morning").
--
-- THE ONE EXCEPTION TO THE WEEKEND RULE. Every other due date the bot computes
-- is shifted off a Saturday or Sunday onto the Monday. These are not: a person
-- asking to be reminded on their own Saturday is making a decision about their
-- own weekend, and a bot that "corrects" it to Monday has thrown the
-- instruction away without saying so.
--
-- One row per reminder rather than one per sheet row: somebody can legitimately
-- want two reminders about the same account.
CREATE TABLE IF NOT EXISTS scheduled_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key      TEXT NOT NULL,      -- normalised "company|poc"; "" when not a row
    company      TEXT NOT NULL DEFAULT '',
    poc          TEXT NOT NULL DEFAULT '',
    due_date     TEXT NOT NULL,      -- YYYY-MM-DD IST, EXACT — never weekend-shifted
    due_time     TEXT NOT NULL DEFAULT '',   -- free text as asked ("morning", "10:00")
    what         TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',   -- open | done | cancelled
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sched_due ON scheduled_reminders(status, due_date);
CREATE INDEX IF NOT EXISTS ix_sched_row ON scheduled_reminders(row_key, status);

-- DRIP SENDS — one row per proactive message that actually went out.
--
-- THIS TABLE IS THE RESTART GUARD, and it replaces the digest's single
-- `sales_digest_date` marker. The digest only had to answer "did today's one
-- message go out"; the drip has to answer "which of today's up-to-three slots
-- went out", because a redeploy at 11:40 must resume at slot 3 rather than
-- starting the day again.
--
-- (on_date, slot) IS UNIQUE, so a double-send is impossible even if two ticks
-- race: the second INSERT fails and the message is not composed again. The slot
-- number is stable across a restart because the schedule is DETERMINISTIC —
-- the jitter is seeded on the date and the slot, not on the clock.
--
-- `group_key` is "<action type>|<owner key>", the same key the grouping uses.
-- `stage` is 'nudge' or 'reask'. Together with `on_date` they are the whole
-- re-ask clock: a group nudged on the 9th is not re-sent until DRIP_REASK_DAYS
-- have passed, then gets exactly ONE 'reask', and after that returns to the
-- normal cadence. Two asks is a reminder; three is nagging.
CREATE TABLE IF NOT EXISTS drip_sends (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    on_date      TEXT NOT NULL,      -- YYYY-MM-DD IST
    slot         INTEGER NOT NULL,   -- 1..n, the order the day's posts went out
    group_key    TEXT NOT NULL,      -- "<type>|<owner key>"
    action_type  TEXT NOT NULL,
    owner_key    TEXT NOT NULL DEFAULT '',
    owner_label  TEXT NOT NULL DEFAULT '',
    companies    TEXT NOT NULL DEFAULT '',   -- comma-separated, as sent
    stage        TEXT NOT NULL DEFAULT 'nudge',   -- nudge | reask
    planned_at   TEXT NOT NULL DEFAULT '',   -- HH:MM IST the schedule asked for
    channel_id   TEXT,
    message_id   TEXT,
    sent_at      TEXT NOT NULL,
    UNIQUE (on_date, slot)
);
CREATE INDEX IF NOT EXISTS ix_drip_group ON drip_sends(group_key, on_date);
CREATE INDEX IF NOT EXISTS ix_drip_date ON drip_sends(on_date);

-- SHEET WRITES — every cell the bot has changed, and what was there before.
--
-- THIS TABLE IS WHAT MAKES WRITING INTO THE TEAM'S LIVE SHEET ACCEPTABLE. The
-- bot changes cells on the strength of a sentence somebody typed in a channel.
-- That is only reasonable if it is trivially reversible by whoever notices, and
-- noticing usually happens the next morning — so the PRIOR VALUE of every cell
-- is stored here and `undo` puts it back exactly.
--
-- ONE ROW PER CELL, grouped by `batch_id`. A reply that fills three cells is
-- three rows with one batch id, because "undo" means undo the whole thing
-- somebody just saw echoed, not one third of it.
--
-- `undone_at` is set rather than the row being deleted. A write that was made
-- and then reversed is a different history from a write that never happened,
-- and audit.jsonl carries both events; throwing the row away would leave the
-- audit trail pointing at a record that no longer exists.
CREATE TABLE IF NOT EXISTS sheet_writes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,      -- one per echoed write; "undo" targets this
    tab          TEXT NOT NULL DEFAULT '',
    sheet_row    INTEGER NOT NULL,
    row_key      TEXT NOT NULL DEFAULT '',   -- normalised "company|poc"
    company      TEXT NOT NULL DEFAULT '',
    poc          TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL,      -- the mapped role, e.g. dm_sent_date
    header       TEXT NOT NULL DEFAULT '',   -- the column's own header text
    cell         TEXT NOT NULL,      -- A1 address, e.g. "M14"
    old_value    TEXT NOT NULL DEFAULT '',
    new_value    TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT '',   -- reply | command
    requested_by TEXT NOT NULL DEFAULT '',
    source_msg   TEXT NOT NULL DEFAULT '',   -- the Discord message that caused it
    written_at   TEXT NOT NULL,      -- ISO timestamp, IST
    undone_at    TEXT NOT NULL DEFAULT '',
    undone_by    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sheet_writes_batch ON sheet_writes(batch_id);
CREATE INDEX IF NOT EXISTS ix_sheet_writes_time ON sheet_writes(written_at);

-- EVENT REMINDERS ALREADY SENT. One row per event, forever.
--
-- ONCE AND ONLY ONCE, and the dedup is PERMANENT rather than per-day. A
-- conference the team has already decided about does not need reminding twice,
-- and "we mentioned it in March" is not a reason to mention it again in April.
--
-- The key is the event name plus its date, so an event that MOVES legitimately
-- earns a fresh reminder — the new date is new information — while re-reading
-- the same row tomorrow does not.
CREATE TABLE IF NOT EXISTS event_reminders (
    event_key   TEXT PRIMARY KEY,   -- "<normalised name>|<YYYY-MM-DD>"
    event       TEXT NOT NULL,
    event_date  TEXT NOT NULL,      -- YYYY-MM-DD IST
    location    TEXT NOT NULL DEFAULT '',
    sent_on     TEXT NOT NULL,      -- YYYY-MM-DD IST the reminder went out
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- SUPPRESS-OR-CONVERT DECISIONS. Every nudge the bot turned into a
-- record-offer, and the evidence that made it do so.
--
-- LOGGED BECAUSE A CONVERSION IS A JUDGEMENT. The bot read a meeting note or a
-- channel message and concluded the thing it was about to ask for has already
-- happened. When that conclusion is wrong the symptom is subtle — a nudge that
-- arrived as a strange offer instead of a question — and without this table
-- there is nothing to look at afterwards. The evidence is stored in the words
-- it was found in.
CREATE TABLE IF NOT EXISTS nudge_conversions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    on_date      TEXT NOT NULL,     -- YYYY-MM-DD IST
    group_key    TEXT NOT NULL DEFAULT '',
    action_type  TEXT NOT NULL,
    company      TEXT NOT NULL DEFAULT '',
    owner_label  TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL,     -- notes | channel
    evidence     TEXT NOT NULL,     -- the sentence that convinced it
    citation     TEXT NOT NULL DEFAULT '',   -- the note + date, or the author + date
    offered      TEXT NOT NULL DEFAULT '',   -- JSON: the fields it offered to record
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_conversions_date ON nudge_conversions(on_date);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R11: WHEN EACH MASTER PIPELINE COMPANY WAS FIRST SEEN.
--
-- THE TAB HAS NO CREATED-DATE COLUMN, so "this company is new" can only be
-- answered by remembering what was there yesterday. One row per company, with
-- the date it first showed up; `pipeline_snapshot` inserts the ones it has not
-- seen and leaves the rest alone, so `first_seen` is genuinely the first time
-- and not the last time the bot looked.
--
-- KEYED ON THE NORMALISED NAME, not the raw cell: "Acme Corp" and "acme corp"
-- are one company, and a case change in the sheet must not read as a new one.
CREATE TABLE IF NOT EXISTS pipeline_companies (
    company_key TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    -- 1 for the rows written by the FIRST EVER snapshot. Those companies were
    -- already on the tab before the bot could see it, so their `first_seen` is
    -- the day the bot started looking, not the day they arrived. They are
    -- remembered so the next run can tell what is genuinely new, and excluded
    -- from every R11 result forever — announcing 273 companies that have been
    -- there for months would be a memorable first impression.
    seeded      INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R5: HOW MANY TIMES A CONTACT HAS BEEN LISTED WITH NOTHING CHANGED.
--
-- The rule asks whether to skip a contact after PROSPECT_REPEAT_ASK_AT posts
-- carrying them unchanged. "Unchanged" is the point: `signature` holds what the
-- row looked like when it was last posted, and a differing signature RESETS the
-- count. Somebody who updated the row deserves a fresh start, not a bot still
-- counting from before they acted.
CREATE TABLE IF NOT EXISTS prospect_mentions (
    row_key     TEXT PRIMARY KEY,
    count       INTEGER NOT NULL DEFAULT 0,
    signature   TEXT NOT NULL DEFAULT '',
    last_date   TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R5: WHICH COMPANIES THIS WEEK'S PROSPECTING IS ALREADY WORKING THROUGH.
--
-- Two companies a week, and the bot stays with one until every contact on it
-- has a first contact recorded. That promise spans days, so the companies in
-- flight are remembered against the ISO week rather than recomputed each run —
-- otherwise Thursday would start two fresh companies and Tuesday's would be
-- abandoned half-contacted.
CREATE TABLE IF NOT EXISTS prospect_week (
    iso_week    TEXT NOT NULL,
    company     TEXT NOT NULL,
    started_on  TEXT NOT NULL,
    PRIMARY KEY (iso_week, company)
);

-- R9: WHICH RUNG OF THE FOLLOW-UP LADDER EACH MEETING IS ON.
--
-- One channel post, then two DMs, then one escalation, then STOP. `sent` is the
-- number of rungs already used and is what decides both the next destination
-- and when to stop for good. `last_date` paces the every-3-days interval.
--
-- ADVANCED BY THE SENDER, NEVER BY THE ENGINE. `nextaction` only reads this —
-- if computing the queue advanced the ladder, every `cadence preview` would
-- burn a rung and a preview would change the thing it was previewing.
-- PERMISSION BEFORE EVERY WRITE: the open proposals.
--
-- A proposal is a write the bot has DESCRIBED and not made. It holds everything
-- needed to apply it later — the row, the cells, and the ORIGINAL reply text —
-- because the approval arrives in a different message from the one that
-- justified it.
--
-- THE ORIGINAL TEXT IS THE POINT OF STORING IT. `said_terminal_words` is
-- matched against what the HUMAN WROTE, not against the extractor's reading of
-- it. Once the write is deferred behind a yes, "yes" is the message in hand and
-- it contains no terminal word at all — so re-deriving consent from the
-- approval would silently disarm the one gate that stops a row being killed by
-- inference.
--
-- `status`: open | applied | declined | expired. Rows are kept after they close
-- so "what did the bot ask and what did we say" has an answer.
CREATE TABLE IF NOT EXISTS write_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key  TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL DEFAULT 'cell_update',
    tab           TEXT NOT NULL DEFAULT '',
    sheet_row     INTEGER,
    row_key       TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    poc           TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '{}',
    reply_text    TEXT NOT NULL DEFAULT '',
    trigger       TEXT NOT NULL DEFAULT '',
    proposed_text TEXT NOT NULL DEFAULT '',
    requested_by  TEXT NOT NULL DEFAULT '',
    channel_id    INTEGER,
    message_id    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    decided_by    TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL DEFAULT '',
    decision      TEXT NOT NULL DEFAULT '',
    nudged_on     TEXT NOT NULL DEFAULT ''
);

-- EVERY ANSWER TO A PROPOSAL, not just the deciding one.
--
-- Two approvers can disagree, and SALES_FINAL_SAY_ID breaks the tie — which
-- means the bot has to remember that Vaishnavi said yes before Sid said no,
-- rather than acting on whichever arrived first and forgetting the other. The
-- decision is computed from ALL the votes, every time one lands.
CREATE TABLE IF NOT EXISTS proposal_votes (
    proposal_key  TEXT NOT NULL,
    voter_id      INTEGER NOT NULL,
    voter_label   TEXT NOT NULL DEFAULT '',
    vote          TEXT NOT NULL,
    voted_at      TEXT NOT NULL DEFAULT '',
    message_id    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (proposal_key, voter_id)
);

-- FOCUS COMMANDS: one row per focus ever set, with an expiry.
--
-- Kept rather than deleted on expiry so "what were we focused on in October"
-- has an answer, and so the one expiry announcement can be made exactly once
-- (`announced_expiry`).
CREATE TABLE IF NOT EXISTS focus (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    field         TEXT NOT NULL DEFAULT '',
    value         TEXT NOT NULL,
    raw           TEXT NOT NULL DEFAULT '',
    set_by        TEXT NOT NULL DEFAULT '',
    set_by_id     INTEGER,
    set_on        TEXT NOT NULL DEFAULT '',
    expires_on    TEXT NOT NULL DEFAULT '',
    cleared_on    TEXT NOT NULL DEFAULT '',
    cleared_by    TEXT NOT NULL DEFAULT '',
    announced_expiry INTEGER NOT NULL DEFAULT 0
);

-- RULE 9's ANSWERS, which the bot may NOT put in the sheet.
--
-- Notes, package and deal size live in Z-AE, the restricted commercial
-- block. R9 asks for them; when somebody answers, the answer is recorded HERE
-- and in the audit log, and the bot says plainly that a human has to put it in
-- the sheet. Recording it is not a substitute for the sheet and is not
-- presented as one — it is so the answer is not lost between being given and
-- being typed in.
CREATE TABLE IF NOT EXISTS meeting_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key       TEXT NOT NULL,
    company       TEXT NOT NULL DEFAULT '',
    poc           TEXT NOT NULL DEFAULT '',
    next_steps    TEXT NOT NULL DEFAULT '',
    package       TEXT NOT NULL DEFAULT '',
    deal_size     TEXT NOT NULL DEFAULT '',
    told_by       TEXT NOT NULL DEFAULT '',
    told_at       TEXT NOT NULL DEFAULT '',
    message_id    TEXT NOT NULL DEFAULT '',
    in_sheet      INTEGER NOT NULL DEFAULT 0
);

-- THE DAILY WEB-SEARCH LEDGER. One row per day, per rule.
--
-- Web search is billed per search on top of tokens, and seven rules can each
-- want several — a runaway day is a real bill. `searches` counts what the API
-- ACTUALLY BILLED (usage.server_tool_use.web_search_requests), not what the bot
-- intended: an errored search is not billed and must not spend the budget.
--
-- PER RULE AS WELL AS PER DAY, so "what spent the budget" has an answer. A
-- day that ran out at 11am because R2 screened forty companies is a different
-- problem from one that ran out because the cap is too low.
CREATE TABLE IF NOT EXISTS web_search_usage (
    on_date     TEXT NOT NULL,
    rule_id     TEXT NOT NULL DEFAULT '',
    searches    INTEGER NOT NULL DEFAULT 0,
    calls       INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (on_date, rule_id)
);

-- EVERY ANTHROPIC CALL, with the API's own token accounting (usage.py).
--
-- `site` is the calling method — parse_query, engine, engine:final,
-- web_research:R1-main, web_research:R1-check … — so "where did the tokens
-- go" is a GROUP BY. cache_write / cache_read are
-- usage.cache_creation_input_tokens / cache_read_input_tokens; input_tokens
-- is the uncached tail only.
CREATE TABLE IF NOT EXISTS llm_calls (
    ts            TEXT NOT NULL,             -- ISO datetime
    site          TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    seconds       REAL NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_ts ON llm_calls(ts);

-- HOW LONG EACH ANSWER TOOK. One row per answered question.
--
-- Here so INTERIM_AFTER_SECONDS / INTERIM_AFTER_WEB_SECONDS can be tuned from
-- data rather than guessed: the "what did you cost" answer reads p50 and p90
-- per route over the last 7 days from this table. `seconds` runs from the
-- moment the gate said yes to the first chunk of the ANSWER (an interim line
-- does not count as the answer). `route` is social | capability | engine |
-- sheet_update.
CREATE TABLE IF NOT EXISTS reply_latency (
    ts           TEXT NOT NULL,              -- ISO datetime, IST
    route        TEXT NOT NULL,
    seconds      REAL NOT NULL,
    used_web     INTEGER NOT NULL DEFAULT 0, -- 1 when a web search ran this turn
    tool_calls   INTEGER NOT NULL DEFAULT 0,
    interim_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_reply_latency_ts ON reply_latency(ts);

-- THE LAST FEW OPENINGS, so the bot does not start five messages the same way.
--
-- Repeating an opening is the single clearest tell that a human is not writing
-- these: three "Worth a look at..." messages in a row and the team stops
-- reading the fourth. The composer is handed the recent openers and told not to
-- reuse them.
--
-- THE KEY IS THE OPENING WITH THE NAME STRIPPED (tone.opener_of). "Vaishnavi —
-- worth a look at Acme" and "Kushal — worth a look at Borealis" are the SAME
-- opening wearing two names, and storing them as different ones would let the
-- bot use one shape every day forever.
CREATE TABLE IF NOT EXISTS message_openers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    opener      TEXT NOT NULL,
    rule_id     TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meeting_followups (
    row_key     TEXT PRIMARY KEY,
    sent        INTEGER NOT NULL DEFAULT 0,
    last_date   TEXT NOT NULL DEFAULT '',
    meeting_date TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R13: WHERE EACH CONNECTED CONTACT STANDS IN THE NEXT-STEP REMINDERS.
--
-- One row per contact Rule 13 has named, keyed like every other per-row ledger
-- (activation.row_key). `signature` is the step the row was on when it was
-- named (the Next Steps value and that step's cells); `mentions`, `calls` and
-- `last_call_date` belong to THAT signature and start again when it changes,
-- which is what "a person keeps coming back until their step changes" means.
-- `last_date` orders the rotation: least recently named goes first.
--
-- `closed` IS NEVER RESET BY A STEP CHANGE. It is set when the reminder to mark
-- the contact Unresponsive goes out, and from then on the rule never names
-- them, whatever the cells later say.
--
-- WRITTEN ONLY BY THE SENDER, after a real send. The evaluator is handed a
-- copy and writes nothing, so a preview cannot move the rotation.
CREATE TABLE IF NOT EXISTS next_step_followups (
    row_key        TEXT PRIMARY KEY,
    signature      TEXT NOT NULL DEFAULT '',
    last_date      TEXT NOT NULL DEFAULT '',
    mentions       INTEGER NOT NULL DEFAULT 0,
    calls          INTEGER NOT NULL DEFAULT 0,
    last_call_date TEXT NOT NULL DEFAULT '',
    last_ask       TEXT NOT NULL DEFAULT '',
    closed         INTEGER NOT NULL DEFAULT 0,
    closed_date    TEXT NOT NULL DEFAULT '',
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R13: WHAT EACH POST SAID, by Discord message id. A reply to the post ("done")
-- has to be matched to a person and a step, and the post's text is not a
-- reliable place to read those back from. `people` is a JSON list in post
-- order. Read by the reply handling (NFT2-1063); written with the state above.
CREATE TABLE IF NOT EXISTS next_step_posts (
    message_id TEXT PRIMARY KEY,
    on_date    TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    people     TEXT NOT NULL DEFAULT '[]'
);

-- R1: WHOSE TURN IT IS — the PoC news rotation (S2).
--
-- One row per name the bot looks up news about: a person on an active Outreach
-- PoCs row (kind 'poc'), or a company on Master Pipeline or Outreach PoCs
-- (kind 'company'). `source` is the tab — or tabs — the name is on, in the
-- sheet's own words; it is what a posted story says in brackets.
--
-- NEWS_POC_TARGETS_PER_DAY names are looked up a day, least recently checked
-- first. `last_searched` is the REAL IST date a name was last in the day's
-- set, '' = never; the day's set is every row stamped with today's date, so
-- every poll of a day asks about the same names and a restart does not move
-- the rotation on.
--
-- (This table was the rotation for the retired people search, which made a
-- billed web search per name. It is reused as it stood: a Google News RSS
-- query per name is free.)
CREATE TABLE IF NOT EXISTS news_targets (
    target_key    TEXT PRIMARY KEY,   -- normalised name + kind
    kind          TEXT NOT NULL,      -- poc | researcher | company
    name          TEXT NOT NULL,      -- as the sheet spells it
    company       TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',   -- which tab the row came from
    sheet_row     INTEGER,
    last_searched TEXT NOT NULL DEFAULT '',   -- ISO date, '' = never
    searches      INTEGER NOT NULL DEFAULT 0,
    hits          INTEGER NOT NULL DEFAULT 0, -- runs that found something
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_news_targets_turn
    ON news_targets(last_searched ASC, kind ASC);

-- R1: WHAT HAS ALREADY BEEN POSTED.
--
-- KEYED ON A NORMALISED URL, not on the title. The same funding round is
-- reported by four outlets with four headlines and often the same canonical
-- link; and the same outlet re-runs its own piece with utm parameters attached.
-- `url_key` strips the scheme, "www.", the query string and any trailing slash,
-- so those collapse to one row. The title is kept for the log and for the
-- report, never for matching.
--
-- WHY A TABLE AND NOT A WINDOW OVER `drip_sends`: a story's identity is its
-- URL, and the sent message is prose. There is nothing in a sent message to
-- match a tomorrow's search result against.
CREATE TABLE IF NOT EXISTS news_stories (
    url_key    TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    about      TEXT NOT NULL DEFAULT '',   -- the person/company it concerned
    rule_id    TEXT NOT NULL DEFAULT '',
    mode       TEXT NOT NULL DEFAULT '',   -- people | field
    posted_on  TEXT NOT NULL,              -- ISO date
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_news_stories_date ON news_stories(posted_on);
-- `topic`, `headline_key`, `importance` and `kind` (main | breaking) were added
-- with the topic feed — see _MIGRATIONS. The headline key is the second half of
-- the no-repeats check: four outlets give one funding round four URLs.

-- R1: THE HOURLY CHECKS THAT HAVE RUN.
--
-- ONE ROW PER (day, slot), claimed BEFORE the search, so a slot runs once a day
-- even across restarts and two ticks in the same minute cannot both search.
-- `posted` is how many stories the check's message carried (0 = it stayed
-- quiet); a row with posted > 0 is one breaking message, which is what the
-- NEWS_BREAKING_MAX_PER_DAY valve counts.
CREATE TABLE IF NOT EXISTS news_checks (
    on_date   TEXT NOT NULL,              -- ISO date
    slot_hhmm TEXT NOT NULL,              -- "16:00", or "test 14:00" from a test day
    ran_at    TEXT NOT NULL,              -- ISO datetime, IST
    searches  INTEGER NOT NULL DEFAULT 0,
    found     INTEGER NOT NULL DEFAULT 0, -- STORY lines that came back
    posted    INTEGER NOT NULL DEFAULT 0, -- stories in the message that went out
    PRIMARY KEY (on_date, slot_hhmm)
);

-- R3: EVENTS THE BOT HAS PROPOSED ADDING.
--
-- COUNTED SO THE PROPOSALS STAY RARE — EVENTS_DISCOVERY_MAX_PER_RUN and
-- EVENTS_DISCOVERY_MAX_PER_MONTH both read this table. It also doubles as the
-- "already proposed" check: an event somebody declined must not come back next
-- fortnight as a fresh discovery, because a proposal nobody accepted is an
-- answer and re-asking it is nagging.
--
-- KEYED ON NAME + DATE, normalised, which is the same key the "is it already on
-- the tab" check uses. Two events with the same name in different months are
-- two events; the same event spelled two ways in the same month is one.
CREATE TABLE IF NOT EXISTS event_discoveries (
    event_key   TEXT PRIMARY KEY,   -- normalised name + ISO date
    name        TEXT NOT NULL,
    event_date  TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL DEFAULT '',
    proposed_on TEXT NOT NULL,      -- ISO date
    month       TEXT NOT NULL,      -- YYYY-MM, for the monthly cap
    status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | added | declined
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_event_discoveries_month ON event_discoveries(month);

-- R3: REGISTRATION DEADLINES THE BOT LOOKED FOR AND DID NOT FIND.
--
-- "I couldn't find the registration deadline for X" is worth saying ONCE.
-- Saying it every other Wednesday about the same six events is how a useful
-- report becomes one people stop reading, and the answer rarely changes inside
-- a fortnight — so a miss is recorded here and not re-asked for
-- EVENTS_DEADLINE_RECHECK_DAYS.
--
-- ONLY MISSES ARE RECORDED. A deadline that was found is written to the sheet,
-- and the sheet is then the answer; a row here for a found deadline would be a
-- second copy of a fact that can change.
CREATE TABLE IF NOT EXISTS event_deadline_checks (
    event_key    TEXT PRIMARY KEY,   -- normalised name + ISO date
    name         TEXT NOT NULL,
    last_checked TEXT NOT NULL,      -- ISO date
    attempts     INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- THE PER-DAY RESEARCH CACHE. One row per researched item per day.
--
-- RESEARCH RUNS AT SEND TIME, for the one message whose slot has come, and
-- this is what stops it running twice. A send that fails after its research,
-- or a restart between the research and the send, would otherwise search again
-- for the same item on the same day and bill the budget for an answer it
-- already had.
--
-- KEYED ON (item_key, on_date). `item_key` is the rule plus the row key (or
-- the company) — see `bot._research_key`. The day is part of the key because
-- yesterday's research is not today's: the news moves and the budget resets.
--
-- ONLY A SEARCH THAT RAN IS CACHED — one that found something or honestly
-- found nothing. "The budget is spent" and "web search is off" are not answers
-- about the item, and caching them would keep an item unresearched after the
-- setting changed.
--
-- `payload_json` carries the other fields the research sets on an item (its
-- rewritten text, R1's mode, R3's proposals) so a hit restores the item
-- exactly rather than approximately.
CREATE TABLE IF NOT EXISTS research_cache (
    item_key     TEXT NOT NULL,
    on_date      TEXT NOT NULL,      -- ISO date
    rule_id      TEXT NOT NULL DEFAULT '',
    research     TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    note         TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_key, on_date)
);

-- THE QUERY -> RESULTS CACHE (search_backend.py), and fetched pages.
--
-- A search request costs money; the same query asked again inside
-- SEARCH_CACHE_HOURS does not. `cache_key` is a hash of the query and its
-- options (or "page|<url>" for a fetched page). `fetched_at` is REAL UTC time,
-- never the pretend clock: the cache is about what the web said recently.
--
-- KEPT BY "reset test state" — wiping it would make the next test day pay
-- again for answers it already had.
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key    TEXT PRIMARY KEY,
    backend      TEXT NOT NULL DEFAULT '',
    query        TEXT NOT NULL DEFAULT '',
    results_json TEXT NOT NULL DEFAULT '[]',
    fetched_at   TEXT NOT NULL              -- ISO datetime, UTC
);

-- A BACKEND'S OWN DAILY QUOTA (search_backend.py). Google's Custom Search API
-- is free for 100 queries a day and billed past it, so every call to it is
-- counted here and the 101st is refused before it leaves. `on_date` is the
-- day the PROVIDER counts in (Pacific time for Google), not the IST day the
-- request budget uses.
--
-- KEPT BY "reset test state": the calls were made whatever a test wipes.
CREATE TABLE IF NOT EXISTS search_quota_usage (
    on_date    TEXT NOT NULL,
    backend    TEXT NOT NULL,
    requests   INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (on_date, backend)
);

-- R1: WHAT THE FEEDS HAVE CARRIED (feeds.py). One row per story.
--
-- A poll reads RSS and stores what is new here; no model and no search API is
-- involved. `url_key` and `headline_key` are the same two identities the
-- posted stories use (news.url_key / news.headline_key), so one story from
-- two feeds is one row. `importance`, `topic` and `what` are filled in later
-- by the light model's scoring call (`scored_at` says when); 0 = not scored.
-- Every timestamp is REAL UTC.
CREATE TABLE IF NOT EXISTS news_feed_items (
    url_key      TEXT PRIMARY KEY,
    headline_key TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',   -- at most 300 characters
    source       TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',   -- ISO datetime, UTC
    topic_hint   TEXT NOT NULL DEFAULT '',   -- the NEWS_TOPICS query it came from
    -- `kind` (industry | poc) and `sheet_ref` ("Synthflow AI — on Master
    -- Pipeline") were added with PoC news — see _MIGRATIONS.
    seen_at      TEXT NOT NULL DEFAULT '',   -- ISO datetime, UTC
    importance   INTEGER NOT NULL DEFAULT 0, -- 1-5 once scored
    topic        TEXT NOT NULL DEFAULT '',
    what         TEXT NOT NULL DEFAULT '',
    scored_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_news_feed_published ON news_feed_items(published_at);
CREATE INDEX IF NOT EXISTS ix_news_feed_headline ON news_feed_items(headline_key);

-- THE VOICE PROFILE: how the team writes, learned from its own messages in the
-- real sales channels (voice.py). ONE ROW, id 1, replaced on every rebuild.
--
-- WHAT IS IN IT: numbers (`stats`), a short style note, and a few example
-- messages with every company and prospect name replaced by a placeholder and
-- every email, phone number and amount removed. NO PROSPECT'S NAME, NO EMAIL,
-- NO NUMBER AND NO DEAL VALUE IS STORED HERE. An example carries its author's
-- Discord id — a team member's, never shown — so "forget my messages" can
-- drop that person's examples.
--
-- `excluded_ids` is the list of team members who said "forget my messages".
-- It survives every rebuild and outlives the profile itself: the row is kept,
-- emptied, when a profile is cleared.
--
-- A simulation runs on a COPY of this database, so it reads the same row the
-- real day and the test day read.
CREATE TABLE IF NOT EXISTS voice_profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    built_at      TEXT NOT NULL DEFAULT '',     -- ISO datetime, REAL UTC; '' = none
    lookback_days INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    author_count  INTEGER NOT NULL DEFAULT 0,
    channels      TEXT NOT NULL DEFAULT '[]',   -- JSON: the channel ids read
    stats         TEXT NOT NULL DEFAULT '{}',   -- JSON: the computed numbers
    exemplars     TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"text", "author_id"}]
    note          TEXT NOT NULL DEFAULT '',     -- "How this team writes"
    note_source   TEXT NOT NULL DEFAULT '',     -- model | rules
    excluded_ids  TEXT NOT NULL DEFAULT '[]'    -- JSON: ids who opted out
);
"""

# WHAT "reset test state" / "start over" KEEP. Everything else is operational
# state and is wiped. These are caches and ledgers of things that were PAID FOR
# (or fetched): deleting them would make the next test run pay again, and would
# make "what did you cost" forget what testing cost. The voice profile is kept
# for both reasons — it cost a model call, and it holds the "forget my
# messages" list, which a test reset must never undo.
KEPT_ON_RESET = ("research_cache", "search_cache", "news_feed_items", "llm_calls",
                 "web_search_usage", "search_quota_usage", "voice_profile",
                 "news_targets")


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` won't
# alter an existing table, so an already-created sales_bot.db needs them
# back-filled. (table, column, DDL) — each added only if missing. Idempotent.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # THE PENDING RECORD-OFFER. When suppress-or-convert turns a nudge into
    # "want me to mark it on the row?", the fields it offered are stored on the
    # drip row — so a reply of "yes" has something concrete to apply. Added
    # after drip_sends first shipped, hence the migration.
    ("drip_sends", "offer", "TEXT NOT NULL DEFAULT ''"),
    # DOES THIS POST TAKE ONE OF THE DAY'S COUNTED POSTS? Written on send, read
    # by `drip.counted_today` — the one counter. Without it the planner asked
    # each sent row whether it counted, got no answer, and counted them all, so
    # a meeting-prep post that had gone out took a slot from a chase after all.
    # NULL on rows from before this: `drip.counts` asks the row's rule instead.
    ("drip_sends", "counts_toward_cap", "INTEGER"),
    # WAS IT A FIXED-TIME POST (R1 at NEWS_MAIN_TIME, R8's day-of touch)? Those
    # take no place in the spaced window, so the planner must not count them
    # when it works out which window time the next post gets. NULL on older rows.
    ("drip_sends", "pinned", "INTEGER"),
    # WHAT WAS POSTED, AND EVERY MESSAGE IT BECAME (NFT2-1063). `body` is the
    # post as it went out, heading included, WITHOUT the tags line and the test
    # tag: "what are today's objectives?" after a post has gone must show that
    # post word for word, and re-composing it would give different words (and
    # ping the people it tags a second time). `part_ids` is a JSON list of the
    # id of every Discord message a long post was split into: the offer a "yes"
    # answers is the LAST line, so a reply to the last part has to find the
    # same post as a reply to the first. Empty on rows from before this.
    ("drip_sends", "body", "TEXT NOT NULL DEFAULT ''"),
    ("drip_sends", "part_ids", "TEXT NOT NULL DEFAULT '[]'"),
    # THE TOPIC FEED. R1 became an AI industry feed on a topic list (24/29 Sep),
    # and a posted story now carries its topic (for the spread rules), its
    # headline key (for the no-repeats check across outlets), the importance
    # the model gave it, and whether it went out in the main post or a
    # breaking one.
    ("news_stories", "topic", "TEXT NOT NULL DEFAULT ''"),
    ("news_stories", "headline_key", "TEXT NOT NULL DEFAULT ''"),
    ("news_stories", "importance", "INTEGER NOT NULL DEFAULT 3"),
    ("news_stories", "kind", "TEXT NOT NULL DEFAULT 'main'"),
    # TWO KINDS OF NEWS (S2). A feed item, and a posted story, is `industry` or
    # `poc`; a PoC one carries the sheet row it is about, in the words the post
    # shows in brackets. On news_stories the column is `news_kind` because
    # `kind` there already says which POST carried it (main | breaking |
    # overflow).
    ("news_feed_items", "kind", "TEXT NOT NULL DEFAULT 'industry'"),
    ("news_feed_items", "sheet_ref", "TEXT NOT NULL DEFAULT ''"),
    ("news_stories", "news_kind", "TEXT NOT NULL DEFAULT 'industry'"),
    ("news_stories", "sheet_ref", "TEXT NOT NULL DEFAULT ''"),
    # ONE-OFF REMINDERS FIRE WHERE THEY WERE ASKED, TAGGING WHO ASKED. Rows from
    # before this carry '' for both: they fire in the posting channel and name
    # the asker in plain text.
    ("scheduled_reminders", "channel_id", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_reminders", "asker_id", "TEXT NOT NULL DEFAULT ''"),
    # WHICH BACKEND A DAY'S SEARCHES RAN ON (searxng | ddg | google_cse |
    # anthropic), so "what did you cost" can price a request. '' on rows from
    # before this is read as anthropic, which is what they were.
    ("web_search_usage", "backend", "TEXT NOT NULL DEFAULT ''"),
]

# Indexes on migrated columns. They cannot live in SCHEMA — on an existing DB
# the columns do not exist until `_migrate` has run — so they are created after
# it. Idempotent.
_POST_MIGRATION_SQL: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_news_stories_headline "
    "ON news_stories(headline_key, posted_on)",
]


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
        log.info("[db] ready at %s", path)

    @staticmethod
    def _migrate(c) -> None:
        """Add any columns introduced after a table first shipped, so an existing
        DB is upgraded in place. Safe on every startup."""
        for table, column, ddl in _MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                log.info("[db] migrating: adding %s.%s", table, column)
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        for sql in _POST_MIGRATION_SQL:
            c.execute(sql)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- chases (the commitments the bot is waiting on) --------------------
    # Read/propose only: a chase produces a REMINDER to a human in the channel
    # the promise was made in, and nothing else.

    def record_chase(
        self,
        *,
        channel_id: int,
        message_id: int,
        jump_url: str,
        person_id: Optional[int],
        person_name: str,
        what: str,
        promised_at: str,
        due_at: str,
    ) -> bool:
        """Remember that `person_name` promised `what`, chaseable from `due_at`.

        Keyed on the promising message, so re-processing that message (an edit, a
        restart replay) can never create a second chase for one promise. Returns
        True when a new row was inserted, False when we already had it."""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO chases
                    (channel_id, message_id, jump_url, person_id, person_name,
                     what, promised_at, due_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    str(channel_id),
                    str(message_id),
                    jump_url,
                    str(person_id) if person_id else None,
                    person_name,
                    what,
                    str(promised_at),
                    str(due_at),
                ),
            )
            return cur.rowcount > 0

    def list_due_chases(self, *, now: str, reminder_cutoff: str) -> list[dict]:
        """Chases ready to be nudged: due at/before `now`, and either never
        nudged or last nudged at/before `reminder_cutoff` (the cooldown). Both
        are UTC 'YYYY-MM-DD HH:MM:SS'. Oldest-due first, so the most overdue
        promise is surfaced before a fresher one."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM chases
                WHERE status = 'open'
                  AND due_at <= ?
                  AND (last_reminded_at IS NULL OR last_reminded_at <= ?)
                ORDER BY due_at ASC
                """,
                (str(now), str(reminder_cutoff)),
            ).fetchall()
            return [self._chase_row(r) for r in rows]

    def list_open_chases(self, limit: int = 50) -> list[dict]:
        """Every open chase, oldest-due first — the "open chases" block in
        state/summary.json, and the honest answer to "what are you waiting on"."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM chases WHERE status = 'open' ORDER BY due_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [self._chase_row(r) for r in rows]

    def find_chase_by_reminder(self, reminder_message_id: int) -> Optional[dict]:
        """The chase a given nudge message is about — so a ✅ on that nudge, or a
        reply to it, closes the right one."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM chases WHERE last_reminder_id = ? AND status = 'open'",
                (str(reminder_message_id),),
            ).fetchone()
            return self._chase_row(row) if row else None

    def find_chase_by_source(self, message_id: int) -> Optional[dict]:
        """The chase created FROM a given message — so a reply to the original
        promise ("here's that deck") closes it too."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM chases WHERE message_id = ? AND status = 'open'",
                (str(message_id),),
            ).fetchone()
            return self._chase_row(row) if row else None

    def mark_chase_reminded(
        self, chase_id: int, *, reminder_message_id: Optional[int], at: str
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE chases
                SET reminders_sent   = reminders_sent + 1,
                    last_reminder_id = ?,
                    last_reminded_at = ?,
                    updated_at       = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    str(reminder_message_id) if reminder_message_id else None,
                    str(at),
                    chase_id,
                ),
            )

    def set_chase_status(self, chase_id: int, status: str) -> None:
        """'closed' (they came back / someone ✅'d it) or 'stale' (asked enough;
        stop). Anything else is a caller bug and is logged as such."""
        if status not in ("open", "closed", "stale"):
            log.warning("[db] unexpected chase status %r for chase %s", status, chase_id)
        with self.conn() as c:
            c.execute(
                "UPDATE chases SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, chase_id),
            )

    def mark_chase_flagged(self, chase_id: int) -> None:
        """Record that this chase has been surfaced as a FLAG (it went stale and
        was reported in-channel), so the flag is raised exactly once."""
        with self.conn() as c:
            c.execute(
                "UPDATE chases SET flagged = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (chase_id,),
            )

    # -- nudges (THE rate limit) -------------------------------------------
    # The bot posts unprompted @-mentions on the chase path. These two methods
    # are the guardrail that keeps that from becoming a nag: every nudge is
    # checked against the log before it is sent, and recorded after.

    def was_nudged_since(
        self, *, target_key: str, subject_key: str, kind: str, since: str
    ) -> bool:
        """Have we already nudged THIS person about THIS thing for THIS reason
        since `since` (a UTC cooldown boundary)? True → stay quiet.

        Callers must fail CLOSED on an exception — treat an unreadable log as
        "assume we did", because nudging twice is worse than missing one."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT 1 FROM nudges
                WHERE target_key = ? AND subject_key = ? AND kind = ? AND sent_at >= ?
                LIMIT 1
                """,
                (str(target_key), str(subject_key), str(kind), str(since)),
            ).fetchone()
            return row is not None

    def record_nudge(
        self,
        *,
        target_key: str,
        subject_key: str,
        kind: str,
        channel_id: Optional[int],
        message_id: Optional[int],
        sent_at: str,
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO nudges
                    (target_key, subject_key, kind, channel_id, message_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(target_key),
                    str(subject_key),
                    str(kind),
                    str(channel_id) if channel_id else None,
                    str(message_id) if message_id else None,
                    str(sent_at),
                ),
            )

    def count_nudges_since(self, since: str) -> int:
        """How many nudges went out since `since` — reported in the daily state
        summary so a supervisor can see the chase volume without reading the
        whole audit log."""
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM nudges WHERE sent_at >= ?", (str(since),)
            ).fetchone()
            return int(row["n"]) if row else 0

    # -- deadlines (authoritative; the sheet is a mirror) ------------------

    @staticmethod
    def company_key(name: str) -> str:
        """Normalised company name used for lookup, so "Emami Ltd.", "emami ltd"
        and "EMAMI  LTD" are one company rather than three deadlines."""
        s = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower())
        s = re.sub(r"\s+", " ", s).strip()
        # Drop the usual legal suffixes — people type them inconsistently.
        s = re.sub(r"\b(pvt|private|ltd|limited|inc|llc|llp|corp|co|plc|gmbh)\b", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def upsert_deadline(
        self,
        *,
        company: str,
        kind: str,
        due_date: str,
        rule: str,
        source: str,
        sheet_row: Optional[int] = None,
        sheet_target: Optional[str] = None,
        sheet_cell: Optional[str] = None,
        owner_id: Optional[int] = None,
        owner_name: Optional[str] = None,
        channel_id: Optional[int] = None,
    ) -> dict:
        """Create or update the deadline for (company, kind).

        THE CONFLICT RULE lives here: an existing 'human' deadline is never
        overwritten by a 'bot' one. The method returns
        {action: created|updated|kept_human, deadline: {...}} so the caller can
        tell what happened and stay quiet when it changed nothing — announcing a
        deadline that was already there is noise.
        """
        key = self.company_key(company)
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM deadlines WHERE company_key = ? AND kind = ?", (key, kind)
            ).fetchone()

            if row is not None:
                existing = self._deadline_row(row)
                # A human's date always wins over anything the bot derives.
                if existing["source"] == "human" and source != "human":
                    return {"action": "kept_human", "deadline": existing}
                if (
                    existing["due_date"] == str(due_date)
                    and existing["source"] == source
                    and existing["status"] == "open"
                ):
                    return {"action": "unchanged", "deadline": existing}
                c.execute(
                    """
                    UPDATE deadlines
                    SET due_date = ?, rule = ?, source = ?, sheet_row = ?,
                        sheet_target = ?, sheet_cell = ?, owner_id = ?, owner_name = ?,
                        channel_id = ?, status = 'open', reminded = 0, chases_sent = 0,
                        escalated = 0, announced = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        str(due_date), rule, source,
                        int(sheet_row) if sheet_row else None,
                        sheet_target, sheet_cell,
                        str(owner_id) if owner_id else None, owner_name,
                        str(channel_id) if channel_id else None,
                        existing["id"],
                    ),
                )
                fresh = c.execute(
                    "SELECT * FROM deadlines WHERE id = ?", (existing["id"],)
                ).fetchone()
                return {"action": "updated", "deadline": self._deadline_row(fresh)}

            cur = c.execute(
                """
                INSERT INTO deadlines
                    (company, company_key, kind, due_date, rule, source, sheet_row,
                     sheet_target, sheet_cell, owner_id, owner_name, channel_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company, key, kind, str(due_date), rule, source,
                    int(sheet_row) if sheet_row else None, sheet_target, sheet_cell,
                    str(owner_id) if owner_id else None, owner_name,
                    str(channel_id) if channel_id else None,
                ),
            )
            fresh = c.execute(
                "SELECT * FROM deadlines WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return {"action": "created", "deadline": self._deadline_row(fresh)}

    def get_deadline(self, company: str, kind: str) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM deadlines WHERE company_key = ? AND kind = ?",
                (self.company_key(company), kind),
            ).fetchone()
            return self._deadline_row(row) if row else None

    def list_deadlines_for(self, company: str) -> list[dict]:
        """Every deadline for one company, soonest first."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM deadlines WHERE company_key = ? ORDER BY due_date ASC",
                (self.company_key(company),),
            ).fetchall()
            return [self._deadline_row(r) for r in rows]

    def list_open_deadlines(self, limit: int = 200) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM deadlines WHERE status = 'open' ORDER BY due_date ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [self._deadline_row(r) for r in rows]

    def list_deadlines_due(self, *, on_or_before: str) -> list[dict]:
        """Open deadlines due on/before an ISO date — the overdue chase list."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM deadlines
                WHERE status = 'open' AND due_date <= ?
                ORDER BY due_date ASC
                """,
                (str(on_or_before),),
            ).fetchall()
            return [self._deadline_row(r) for r in rows]

    def list_deadlines_on(self, *, due_date: str, reminded: bool = False) -> list[dict]:
        """Open deadlines falling on one date — used for the one-working-day-
        before reminder, filtered to those not yet reminded."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM deadlines
                WHERE status = 'open' AND due_date = ? AND reminded = ?
                ORDER BY company ASC
                """,
                (str(due_date), 1 if reminded else 0),
            ).fetchall()
            return [self._deadline_row(r) for r in rows]

    def mark_deadline(self, deadline_id: int, **fields) -> None:
        """Set any of announced / reminded / escalated / status / sheet_cell /
        sheet_target, or bump chases_sent. Unknown fields are ignored with a
        warning rather than silently dropped."""
        allowed = {
            "announced", "reminded", "escalated", "status", "sheet_cell",
            "sheet_target", "due_date", "rule", "source", "owner_id", "owner_name",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key == "bump_chases":
                sets.append("chases_sent = chases_sent + 1")
                continue
            if key not in allowed:
                log.warning("[db] mark_deadline ignoring unknown field %r", key)
                continue
            sets.append(f"{key} = ?")
            params.append(int(value) if isinstance(value, bool) else value)
        if not sets:
            return
        params.append(deadline_id)
        with self.conn() as c:
            c.execute(
                f"UPDATE deadlines SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                params,
            )

    # -- row-hygiene flags (once a day, per row, per kind) -----------------

    def flag_already_sent(self, *, flag_kind: str, company: str, on_date: str) -> bool:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM flags_sent WHERE flag_kind = ? AND company_key = ? "
                "AND on_date = ? LIMIT 1",
                (flag_kind, self.company_key(company), str(on_date)),
            ).fetchone()
            return row is not None

    def record_flag(
        self,
        *,
        flag_kind: str,
        company: str,
        on_date: str,
        channel_id: Optional[int],
        message_id: Optional[int],
        sent_at: str,
    ) -> bool:
        """Record a raised flag. Returns False when one was already recorded for
        that row/kind/day — the INSERT OR IGNORE is what makes "once a day" true
        even if two sweeps race."""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO flags_sent
                    (flag_kind, company_key, on_date, channel_id, message_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    flag_kind, self.company_key(company), str(on_date),
                    str(channel_id) if channel_id else None,
                    str(message_id) if message_id else None,
                    str(sent_at),
                ),
            )
            return cur.rowcount > 0

    # -- digest carry-forward (the "(3rd day)" marker) ---------------------
    # The ONE daily digest repeats whatever is still outstanding, with its age.
    # These two methods are the whole mechanism: `note_digest_item` is called
    # once per item while the digest is being BUILT, and `prune_digest_items`
    # forgets items that stopped appearing (i.e. got resolved).

    def note_digest_item(
        self, *, item_key: str, section: str, on_date: str, gap_days: int = 7
    ) -> int:
        """Record that `item_key` is in today's digest; return which day it is on.

        1 on the first appearance, 2 the next day it appears, and so on — the
        number `digest.age_note` turns into "(3rd day)".

        IDEMPOTENT WITHIN A DAY. Called twice for the same date (a retry after a
        refused send, two sweeps racing) it returns the same number rather than
        ageing the item twice. That matters because the digest is built before it
        is sent, so a failed send genuinely does re-run this.

        `gap_days` resets the count when an item vanished for a while and came
        back: a deal that went quiet in March and stalled again in August is on
        its first day of THIS problem, not its ninetieth.
        """
        today = str(on_date)
        with self.conn() as c:
            row = c.execute(
                "SELECT first_seen, last_seen, times_seen FROM digest_items WHERE item_key = ?",
                (str(item_key),),
            ).fetchone()

            if row is None:
                c.execute(
                    """
                    INSERT INTO digest_items (item_key, section, first_seen, last_seen, times_seen)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (str(item_key), str(section), today, today),
                )
                return 1

            last_seen = str(row["last_seen"] or "")
            times = int(row["times_seen"] or 1)

            if last_seen == today:
                # Already counted today. Refresh the section (an item can move
                # from OVERDUE to ESCALATIONS) but never the count.
                c.execute(
                    "UPDATE digest_items SET section = ? WHERE item_key = ?",
                    (str(section), str(item_key)),
                )
                return times

            if _days_between(last_seen, today) > max(1, int(gap_days)):
                c.execute(
                    """
                    UPDATE digest_items
                    SET section = ?, first_seen = ?, last_seen = ?, times_seen = 1
                    WHERE item_key = ?
                    """,
                    (str(section), today, today, str(item_key)),
                )
                log.info(
                    "[db] digest item %s reappeared after %s — age restarted at day 1",
                    item_key, last_seen,
                )
                return 1

            times += 1
            c.execute(
                """
                UPDATE digest_items
                SET section = ?, last_seen = ?, times_seen = ?
                WHERE item_key = ?
                """,
                (str(section), today, times, str(item_key)),
            )
            return times

    def prune_digest_items(self, *, before_date: str) -> int:
        """Forget digest items not seen since `before_date` (an ISO IST date).

        Housekeeping, not logic: an item that stops appearing has been resolved,
        and `note_digest_item`'s gap rule already handles one that comes back
        before this ever runs. Returns how many rows were dropped.
        """
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM digest_items WHERE last_seen < ?", (str(before_date),)
            )
            return cur.rowcount or 0

    # -- meta (the bot's own bookkeeping) ----------------------------------

    # -- cadence: the Next Steps clock -------------------------------------

    @staticmethod
    def nextstep_key(company: str, poc: str = "") -> str:
        """The stable identity of a tracked row: company + PoC, normalised.

        NOT the sheet row number. Rows get inserted above and sorted, and a key
        that moved with them would reset the clock on every reorder — which is
        exactly the failure rule (i) exists to catch.
        """
        return f"{DB.company_key(company)}|{DB.company_key(poc)}"

    def track_next_steps(self, *, row_key: str, text: str, on_date: str) -> int:
        """Record what the Next Steps cell says today; return DAYS UNCHANGED.

        First sighting returns 0 — a next step the bot has never seen before is
        not stale, it is new. The count only starts once there is a previous
        observation to compare against, which means a fresh database earns its
        rule-(i) findings over the following days rather than firing a wall of
        them on day one.

        An edited cell resets the clock: new text, new `first_seen`.
        """
        import hashlib

        clean = " ".join(str(text or "").split()).strip().lower()
        if not clean:
            # Blank Next Steps is a different rule's business (f / h). Forget any
            # previous text so re-entering the SAME text later starts a new clock.
            with self.conn() as c:
                c.execute("DELETE FROM nextstep_state WHERE row_key = ?", (row_key,))
            return 0

        digest_hash = hashlib.sha1(clean.encode("utf-8")).hexdigest()
        with self.conn() as c:
            row = c.execute(
                "SELECT text_hash, first_seen FROM nextstep_state WHERE row_key = ?",
                (row_key,),
            ).fetchone()
            if row is None or row["text_hash"] != digest_hash:
                c.execute(
                    "INSERT INTO nextstep_state "
                    "(row_key, text_hash, text_sample, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(row_key) DO UPDATE SET "
                    "  text_hash = excluded.text_hash, "
                    "  text_sample = excluded.text_sample, "
                    "  first_seen = excluded.first_seen, "
                    "  last_seen = excluded.last_seen",
                    (row_key, digest_hash, str(text or "")[:200], on_date, on_date),
                )
                return 0
            c.execute(
                "UPDATE nextstep_state SET last_seen = ? WHERE row_key = ?",
                (on_date, row_key),
            )
            first_seen = str(row["first_seen"] or on_date)

        try:
            from datetime import date as _date

            return max(0, (_date.fromisoformat(on_date) - _date.fromisoformat(first_seen)).days)
        except ValueError:
            log.debug("[db] unparseable next-step dates %r/%r", on_date, first_seen)
            return 0

    def prune_next_steps(self, *, before_date: str) -> int:
        """Forget rows not seen since `before_date` — a deleted sheet row should
        not keep a clock running forever."""
        with self.conn() as c:
            cur = c.execute("DELETE FROM nextstep_state WHERE last_seen < ?", (before_date,))
            return int(cur.rowcount or 0)

    # -- cadence: meeting-prep brief dedup ----------------------------------

    @staticmethod
    def prep_key(company: str, poc: str, meeting_date: str) -> str:
        """One brief per company + PoC + meeting DATE. A rescheduled meeting is a
        different meeting and earns a new brief; the same one re-read tomorrow
        does not."""
        return f"{DB.company_key(company)}|{DB.company_key(poc)}|{str(meeting_date or '').strip()}"

    def prep_brief_sent(self, meeting_key: str) -> bool:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM prep_briefs WHERE meeting_key = ?", (meeting_key,)
            ).fetchone()
            return row is not None

    def record_prep_brief(
        self, *, meeting_key: str, company: str, poc: str,
        meeting_date: str, sent_on: str,
    ) -> None:
        """Mark a brief as written. Called only AFTER the digest actually posts —
        a refused send must not consume the one brief a meeting gets."""
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO prep_briefs "
                "(meeting_key, company, poc, meeting_date, sent_on) "
                "VALUES (?, ?, ?, ?, ?)",
                (meeting_key, company, poc, meeting_date, sent_on),
            )
        log.info(
            "[db] prep brief recorded for %s / %s on %s", company, poc or "(no PoC)", meeting_date
        )

    # -- sheet-health flags, deduped until they change ---------------------

    def quality_flag_seen(self, flag_key: str, signature: str) -> bool:
        """Has this exact finding already been reported, unchanged?

        True means DON'T repeat it. The comparison is on the SIGNATURE, not on
        the date: a broken formula that is still broken tomorrow is not news,
        and a broken formula that has grown from one column to three is.

        Fails OPEN — a database error returns False, so the flag is reported.
        The cost of saying it twice is one line; the cost of never saying it is
        a column of #REF! nobody hears about.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT signature FROM quality_flags WHERE flag_key = ?",
                    (str(flag_key),),
                ).fetchone()
        except Exception:
            log.debug("[db] quality flag lookup failed for %r", flag_key, exc_info=True)
            return False
        return bool(row) and str(row["signature"]) == str(signature)

    def record_quality_flag(self, *, flag_key: str, signature: str, on_date: str) -> None:
        """Remember that this finding was reported, with what it said."""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE quality_flags SET signature = ?, last_seen = ?, "
                "times_seen = times_seen + 1 WHERE flag_key = ?",
                (str(signature), str(on_date), str(flag_key)),
            )
            if cur.rowcount == 0:
                c.execute(
                    "INSERT INTO quality_flags (flag_key, signature, first_seen, "
                    "last_seen, times_seen) VALUES (?, ?, ?, ?, 1)",
                    (str(flag_key), str(signature), str(on_date), str(on_date)),
                )

    # -- explicit row activations -----------------------------------------
    # The named exception to the first-contact/connection-date rule. See the
    # row_activations comment in SCHEMA, and activation.py for the rule itself.

    def activate_row(
        self, *, row_key: str, company: str, poc: str = "", reason: str = "",
        requested_by: str = "", on_date: str = "",
    ) -> bool:
        """Remember that somebody explicitly activated this row.

        Returns True when this was a NEW activation, False when it was already
        active — the caller says "already on" rather than reporting a change it
        did not make.

        The first activation's wording is kept rather than overwritten: the
        reason a row became visible is a fact about a moment, and a later
        re-request is not a correction of it.
        """
        key = str(row_key or "").strip()
        if not key or key == "|":
            log.warning("[db] refusing to activate a row with no company or PoC")
            return False
        with self.conn() as c:
            existing = c.execute(
                "SELECT row_key FROM row_activations WHERE row_key = ?", (key,)
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO row_activations (row_key, company, poc, reason, "
                "requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, str(company or ""), str(poc or ""), str(reason or ""),
                 str(requested_by or ""), str(on_date or "")),
            )
        log.info("[db] row ACTIVATED: %s (%s)", key, reason or "no reason given")
        return True

    def activated_row_keys(self) -> frozenset:
        """Every explicitly activated row key.

        FAILS CLOSED — a database error returns an EMPTY set, so a row falls
        back to the date rule rather than becoming visible on the strength of a
        query that did not run. Wrongly quiet is recoverable; wrongly chasing a
        stranger is not.
        """
        try:
            with self.conn() as c:
                rows = c.execute("SELECT row_key FROM row_activations").fetchall()
        except Exception:
            log.exception("[db] could not read the row activations; treating as none")
            return frozenset()
        return frozenset(str(r["row_key"]) for r in rows)

    def list_activated_rows(self, limit: int = 200) -> list[dict]:
        """The activations, newest first, for the "sheet status" answer."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT row_key, company, poc, reason, requested_by, created_at "
                "FROM row_activations ORDER BY created_at DESC, company ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def deactivate_row(self, row_key: str) -> bool:
        """Undo one explicit activation. True when there was one to undo."""
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM row_activations WHERE row_key = ?", (str(row_key or ""),)
            )
        if cur.rowcount:
            log.info("[db] row activation removed: %s", row_key)
        return bool(cur.rowcount)

    # -- snoozes and explicitly scheduled reminders ------------------------
    # The two tables the next-action state machine reads as INPUT. Neither is
    # written by the engine: it is pure computation, and a computation that
    # wrote to the database on every run would make "cadence preview" a side
    # effect rather than a preview.

    def set_snooze(
        self, *, row_key: str, company: str, poc: str = "", until_date: str,
        note: str = "", requested_by: str = "", on_date: str = "",
    ) -> dict:
        """Snooze one row until `until_date`. Replaces any existing snooze.

        Returns {action: created|moved|refused, previous: <old date or "">} so
        the caller can say "moved from the 12th to the 20th" rather than
        reporting a change it cannot describe. Replacing rather than refusing is
        right: the most recent instruction is the live one.
        """
        key = str(row_key or "").strip()
        if not key or key == "|":
            log.warning("[db] refusing to snooze a row with no company or PoC")
            return {"action": "refused", "previous": ""}
        with self.conn() as c:
            row = c.execute(
                "SELECT until_date FROM snoozes WHERE row_key = ?", (key,)
            ).fetchone()
            previous = str(row["until_date"]) if row else ""
            c.execute(
                "INSERT INTO snoozes (row_key, company, poc, until_date, note, "
                "requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(row_key) DO UPDATE SET until_date = excluded.until_date, "
                "note = excluded.note, requested_by = excluded.requested_by, "
                "created_at = excluded.created_at",
                (key, str(company or ""), str(poc or ""), str(until_date),
                 str(note or ""), str(requested_by or ""), str(on_date or "")),
            )
        log.info("[db] snooze %s until %s (%s)", key, until_date, note or "no note")
        return {"action": "moved" if previous else "created", "previous": previous}

    def snoozes(self) -> dict:
        """{row_key: {row_key, company, poc, until_date, note, requested_by}}.

        FAILS OPEN — a database error returns {}, so a row falls back to its
        normal trigger rather than being silenced by a query that did not run.
        Losing a snooze costs one line in a preview; honouring one that isn't
        there costs a row nobody ever chases again.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT row_key, company, poc, until_date, note, requested_by "
                    "FROM snoozes"
                ).fetchall()
        except Exception:
            log.exception("[db] could not read the snoozes; treating as none")
            return {}
        return {str(r["row_key"]): dict(r) for r in rows}

    def list_snoozes(self, limit: int = 200) -> list[dict]:
        """The snoozes, soonest first, for the preview's footer."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT row_key, company, poc, until_date, note, requested_by, "
                "created_at FROM snoozes ORDER BY until_date ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_snooze(self, row_key: str) -> bool:
        """Remove one snooze. True when there was one to remove."""
        with self.conn() as c:
            cur = c.execute("DELETE FROM snoozes WHERE row_key = ?", (str(row_key or ""),))
        if cur.rowcount:
            log.info("[db] snooze cleared: %s", row_key)
        return bool(cur.rowcount)

    def add_scheduled_reminder(
        self, *, row_key: str = "", company: str = "", poc: str = "",
        due_date: str, due_time: str = "", what: str, requested_by: str = "",
        on_date: str = "", channel_id: str = "", asker_id: str = "",
    ) -> int:
        """Record a one-off reminder somebody asked for at a specific time.

        Returns its id. These are the ONLY dates exempt from the weekend shift —
        see the table comment in SCHEMA.
        """
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO scheduled_reminders (row_key, company, poc, due_date, "
                "due_time, what, requested_by, status, created_at, channel_id, "
                "asker_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (str(row_key or ""), str(company or ""), str(poc or ""), str(due_date),
                 str(due_time or ""), str(what), str(requested_by or ""),
                 str(on_date or ""), str(channel_id or ""), str(asker_id or "")),
            )
            new_id = int(cur.lastrowid)
        log.info(
            "[db] scheduled reminder #%d for %s on %s%s: %s",
            new_id, company or row_key or "(no row)", due_date,
            f" {due_time}" if due_time else "", what,
        )
        return new_id

    def scheduled_reminders_by_row(self) -> dict:
        """{row_key: [reminder, ...]} for every OPEN reminder attached to a row,
        soonest first.

        FAILS OPEN, like `snoozes()`: a database error returns {} and the rows
        keep their normal triggers.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT id, row_key, company, poc, due_date, due_time, what, "
                    "requested_by FROM scheduled_reminders "
                    "WHERE status = 'open' AND row_key != '' ORDER BY due_date ASC"
                ).fetchall()
        except Exception:
            log.exception("[db] could not read the scheduled reminders; treating as none")
            return {}
        out: dict = {}
        for r in rows:
            out.setdefault(str(r["row_key"]), []).append(dict(r))
        return out

    def list_scheduled_reminders(self, limit: int = 200,
                                 asker_id: str = "") -> list[dict]:
        """Every OPEN reminder, soonest first — including ones not tied to a row.
        `asker_id` narrows it to one person's."""
        sql = ("SELECT id, row_key, company, poc, due_date, due_time, what, "
               "requested_by, created_at, channel_id, asker_id "
               "FROM scheduled_reminders WHERE status = 'open'")
        params: list = []
        if asker_id:
            sql += " AND asker_id = ?"
            params.append(str(asker_id))
        sql += " ORDER BY due_date ASC, due_time ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def scheduled_reminders_due_by(self, on_date: str) -> list[dict]:
        """OPEN reminders dated `on_date` OR EARLIER, for the exact-time loop —
        the only thing that sends a reminder.

        "OR EARLIER" IS THE CATCH-UP. A reminder whose date passed while the bot
        was down, or that was written with yesterday's date, used to be left to
        the drip's second lane, which never closed it. The loop now takes it on
        its next tick, says when it was due, and closes it.

        FAILS QUIET: an unreadable table means no reminder fires this minute,
        and the next tick tries again."""
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT id, row_key, company, poc, due_date, due_time, what, "
                    "requested_by, channel_id, asker_id FROM scheduled_reminders "
                    "WHERE status = 'open' AND due_date <= ? "
                    "ORDER BY due_date, id",
                    (str(on_date),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            log.exception("[reminders] could not read today's reminders")
            return []

    def scheduled_reminder(self, reminder_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM scheduled_reminders WHERE id = ?",
                            (int(reminder_id),)).fetchone()
        return dict(row) if row else None

    def reopen_scheduled_reminder(self, reminder_id: int) -> None:
        """Put a claimed reminder back to open — its post failed, so the next
        tick tries again."""
        with self.conn() as c:
            c.execute("UPDATE scheduled_reminders SET status = 'open' "
                      "WHERE id = ? AND status = 'done'", (int(reminder_id),))

    def close_scheduled_reminder(self, reminder_id: int, status: str = "done") -> bool:
        """Mark one reminder done or cancelled. True when it was open."""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE scheduled_reminders SET status = ? WHERE id = ? AND status = 'open'",
                (str(status), int(reminder_id)),
            )
        return bool(cur.rowcount)

    # -- the drip -----------------------------------------------------------
    # The per-day restart guard and the re-ask clock. See the drip_sends comment
    # in SCHEMA for why this is a table of slots rather than one date marker.

    def drip_sent_today(self, on_date: str) -> list[dict]:
        """Every message already sent today, in slot order.

        THE RESTART GUARD. The scheduler recomputes the whole day's plan on
        every tick — deterministically — and skips the slots this returns. A
        redeploy at 11:40 therefore resumes at slot 3 instead of replaying the
        morning.

        FAILS CLOSED: a database error returns a sentinel that the caller treats
        as "I cannot tell what has gone out", and it sends NOTHING. A duplicate
        nudge is worse than a missed one, and the failure is loud in the log.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT slot, group_key, action_type, owner_key, owner_label, "
                "companies, stage, planned_at, sent_at, counts_toward_cap, pinned, "
                "body, message_id, channel_id, part_ids "
                "FROM drip_sends WHERE on_date = ? ORDER BY slot ASC",
                (str(on_date),),
            ).fetchall()
        return [self._drip_row(r) for r in rows]

    @staticmethod
    def _drip_row(row) -> dict:
        """One drip_sends row as a dict, with `part_ids` as a list of strings.
        Stored as JSON; a row from before the column, or one somebody edited
        by hand, reads as [] rather than failing the lookup it is part of."""
        import json as _json
        out = dict(row)
        if "part_ids" in out:
            try:
                parts = _json.loads(out.get("part_ids") or "[]")
            except (ValueError, TypeError):
                parts = []
            out["part_ids"] = [str(p) for p in parts if str(p or "").strip()] \
                if isinstance(parts, list) else []
        return out

    def record_drip_send(
        self, *, on_date: str, slot: int, group_key: str, action_type: str,
        owner_key: str = "", owner_label: str = "", companies: str = "",
        stage: str = "nudge", planned_at: str = "", channel_id=None,
        message_id=None, sent_at: str = "", counts_toward_cap: bool = True,
        pinned: bool = False,
    ) -> bool:
        """Record that one drip message went out. False when that slot was
        already taken.

        `counts_toward_cap` and `pinned` are the post's own answers to "did this
        take one of the day's counted posts" and "was it a fixed-time post".
        They are stored, not re-derived, so a later change to bot_rules.yaml
        cannot rewrite what an earlier post cost the day.

        The UNIQUE (on_date, slot) constraint is doing real work: two sweep
        ticks racing on the same slot both try to insert, one wins, and the
        loser does not send. It is cheaper and more certain than a lock.
        """
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO drip_sends (on_date, slot, group_key, action_type, "
                    "owner_key, owner_label, companies, stage, planned_at, channel_id, "
                    "message_id, sent_at, counts_toward_cap, pinned) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(on_date), int(slot), str(group_key), str(action_type),
                     str(owner_key or ""), str(owner_label or ""), str(companies or ""),
                     str(stage or "nudge"), str(planned_at or ""),
                     str(channel_id) if channel_id else None,
                     str(message_id) if message_id else None, str(sent_at or ""),
                     1 if counts_toward_cap else 0, 1 if pinned else 0),
                )
        except sqlite3.IntegrityError:
            log.warning(
                "[drip] slot %s on %s was already recorded — not sending it twice",
                slot, on_date,
            )
            return False
        return True

    def drip_group_history(self, lookback_days: int = 30) -> dict:
        """{group_key: {last_sent, last_stage, last_nudge, last_reask}}.

        THE RE-ASK CLOCK. `last_nudge` is when the group was last asked about
        fresh; `last_reask` is when its one gentle follow-up went out, if it
        did. The scheduler reads both to decide whether a group is (a) too
        recently asked to repeat, (b) due its single re-ask, or (c) back on the
        normal cadence.

        FAILS OPEN — an error returns {}, so every group looks fresh. That
        direction is right for a clock whose whole job is to SUPPRESS: losing it
        costs one extra message, and inverting it would silence a group forever.
        """
        # THE CUTOFF IS AN IST DATE, computed here. SQLite's date('now') is the
        # UTC date, and `on_date` is an IST date: between 00:00 and 05:30 IST
        # the two disagree by a day. Through the bot's clock, so a pretend day
        # counts back from the pretend date.
        import deadlines as _dl
        cutoff = _dl.iso(_dl.today_ist() - timedelta(days=max(1, int(lookback_days))))
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT group_key, stage, MAX(on_date) AS last_date FROM drip_sends "
                    "WHERE on_date >= ? GROUP BY group_key, stage",
                    (cutoff,),
                ).fetchall()
        except Exception:
            log.exception("[db] could not read the drip history; treating every group as new")
            return {}
        out: dict = {}
        for r in rows:
            key = str(r["group_key"])
            entry = out.setdefault(key, {"last_nudge": "", "last_reask": "", "last_sent": ""})
            when = str(r["last_date"] or "")
            if str(r["stage"]) == "reask":
                entry["last_reask"] = max(entry["last_reask"], when)
            else:
                entry["last_nudge"] = max(entry["last_nudge"], when)
            entry["last_sent"] = max(entry["last_sent"], when)
        return out

    def attach_drip_message_id(self, *, on_date: str, slot: int, message_id,
                               body: str = "", part_ids=()) -> bool:
        """Record which Discord message a drip slot became, what it said and
        every part it was split into.

        The slot row is written BEFORE the send (that is what claims it against
        a racing tick), so the message id can only be filled in afterwards. It
        matters because a REPLY to a drip message is one of the two things that
        may write to the sheet, and `find_drip_by_message_id` is how the reply
        finds out which companies that nudge was about.

        `body` is the post as it went out, without the tags line and the test
        tag — what the on-demand objectives answer shows once the post has gone
        (NFT2-1063). `part_ids` is every Discord message the post became, so a
        reply to its last part resolves to the same row. Both are optional and
        a caller that passes neither leaves those columns as they were.
        """
        import json as _json
        parts = [str(p) for p in (part_ids or ()) if str(p or "").strip()]
        sets, args = ["message_id = ?"], [str(message_id)]
        if str(body or "").strip():
            sets.append("body = ?")
            args.append(str(body))
        if parts:
            sets.append("part_ids = ?")
            args.append(_json.dumps(parts))
        with self.conn() as c:
            cur = c.execute(
                f"UPDATE drip_sends SET {', '.join(sets)} WHERE on_date = ? AND slot = ?",
                (*args, str(on_date), int(slot)),
            )
        return bool(cur.rowcount)

    def find_drip_by_message_id(self, message_id: str) -> Optional[dict]:
        """The drip row a Discord message id belongs to, or None.

        THE CONTEXT FOR A REPLY. "Sent this morning" names no company and no
        column; the nudge it answers names both, and this is the lookup that
        connects them. None simply means the reply was to something else the bot
        said, and the extractor works from the reply alone.

        A LATER PART OF A SPLIT POST FINDS THE SAME ROW (NFT2-1063). A long
        post goes out as several Discord messages and the offer a "yes" answers
        is its last line. The row's `message_id` is always the FIRST part's id
        — the one every proposal is keyed to — whichever part was replied to.
        """
        mid = str(message_id or "").strip()
        if not mid:
            return None
        cols = ("on_date, slot, group_key, action_type, owner_label, companies, "
                "stage, offer, body, message_id, channel_id, part_ids")
        try:
            with self.conn() as c:
                row = c.execute(
                    f"SELECT {cols} FROM drip_sends WHERE message_id = ?", (mid,),
                ).fetchone()
                if row is None:
                    # The id sits inside a JSON list of strings, so the quoted
                    # form is matched; the Python check below is the real test.
                    maybe = c.execute(
                        f"SELECT {cols} FROM drip_sends WHERE part_ids LIKE ? "
                        "ORDER BY id DESC", (f'%"{mid}"%',),
                    ).fetchall()
                    row = next((r for r in maybe
                                if mid in self._drip_row(r)["part_ids"]), None)
        except Exception:
            log.exception("[db] could not look up the drip message %r", message_id)
            return None
        return self._drip_row(row) if row else None

    def list_drip_sends(self, limit: int = 50) -> list[dict]:
        """The most recent drip messages, newest first. For the audit answer."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT on_date, slot, group_key, action_type, owner_label, companies, "
                "stage, planned_at, sent_at FROM drip_sends "
                "ORDER BY on_date DESC, slot DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_drip_sends(self, *, before_date: str) -> int:
        """Drop drip rows older than `before_date`. Returns how many went."""
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM drip_sends WHERE on_date < ?", (str(before_date),)
            )
        return int(cur.rowcount or 0)

    # -- sheet writes and their undo ---------------------------------------
    # See the sheet_writes comment in SCHEMA. The short version: the bot writes
    # into the sheet the team actually works in, so every cell it changes is
    # recorded with what was there before, and anyone can put it back.

    def record_sheet_write(
        self, *, batch_id: str, tab: str, sheet_row: int, row_key: str = "",
        company: str = "", poc: str = "", cells: list, trigger: str = "",
        requested_by: str = "", source_msg: str = "", written_at: str = "",
    ) -> int:
        """Record one batch of written cells. Returns how many rows were stored.

        `cells` is what `gtm_sheet.write_cells` returned in `written` — each
        entry already carries the OLD value, which is the whole point.
        """
        rows = [
            (str(batch_id), str(tab or ""), int(sheet_row), str(row_key or ""),
             str(company or ""), str(poc or ""), str(c.get("role") or ""),
             str(c.get("header") or ""), str(c.get("cell") or ""),
             str(c.get("old") or ""), str(c.get("new") or ""),
             str(trigger or ""), str(requested_by or ""), str(source_msg or ""),
             str(written_at or ""))
            for c in (cells or [])
        ]
        if not rows:
            return 0
        with self.conn() as c:
            c.executemany(
                "INSERT INTO sheet_writes (batch_id, tab, sheet_row, row_key, company, "
                "poc, role, header, cell, old_value, new_value, trigger, requested_by, "
                "source_msg, written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        log.info("[db] recorded %d written cell(s) as batch %s", len(rows), batch_id)
        return len(rows)

    def latest_undoable_write(self, *, within_hours: int, now_iso: str) -> Optional[dict]:
        """The most recent batch still inside the undo window, or None.

        "UNDO" WITH NO TARGET MEANS THE LAST ONE. That is what people mean when
        they type it, and asking "which one?" of somebody who has just spotted a
        wrong cell is the wrong response — they want it gone now.

        A batch that has already been undone is not offered again: undoing an
        undo would re-apply the write, which is never what the word means.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT batch_id, MAX(written_at) AS at FROM sheet_writes "
                    "WHERE undone_at = '' GROUP BY batch_id ORDER BY at DESC LIMIT 1"
                ).fetchone()
        except Exception:
            log.exception("[db] could not look up the last sheet write")
            return None
        if not row or not row["batch_id"]:
            return None
        batch = self.sheet_write_batch(str(row["batch_id"]))
        if not batch:
            return None
        age = _hours_between(str(row["at"] or ""), str(now_iso))
        if age is None or age > max(0, int(within_hours)):
            log.info(
                "[db] the last sheet write (batch %s) is %s old — past the %dh undo "
                "window", row["batch_id"],
                f"{age:.1f}h" if age is not None else "of unknown age", within_hours,
            )
            return None
        return {"batch_id": str(row["batch_id"]), "cells": batch,
                "age_hours": age}

    def sheet_write_batch(self, batch_id: str) -> list[dict]:
        """Every cell in one batch, in the order it was written."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM sheet_writes WHERE batch_id = ? ORDER BY id ASC",
                (str(batch_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_sheet_write_undone(
        self, *, batch_id: str, undone_by: str = "", undone_at: str = ""
    ) -> int:
        """Mark a batch reverted. Returns how many rows were marked.

        The rows stay. A write that was made and then reversed is a different
        history from a write that never happened, and audit.jsonl carries both.
        """
        with self.conn() as c:
            cur = c.execute(
                "UPDATE sheet_writes SET undone_at = ?, undone_by = ? "
                "WHERE batch_id = ? AND undone_at = ''",
                (str(undone_at or ""), str(undone_by or ""), str(batch_id)),
            )
        return int(cur.rowcount or 0)

    def list_sheet_writes(self, limit: int = 50) -> list[dict]:
        """The most recent written cells, newest first. For the audit answer."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT batch_id, company, poc, role, header, cell, old_value, "
                "new_value, trigger, requested_by, written_at, undone_at, undone_by "
                "FROM sheet_writes ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- event reminders and conversion decisions --------------------------

    @staticmethod
    def event_key(event: str, event_date: str) -> str:
        """"<normalised name>|<date>". An event that MOVES earns a fresh
        reminder — the new date is new information — while re-reading the same
        row tomorrow does not."""
        name = re.sub(r"[^a-z0-9]+", " ", str(event or "").lower()).strip()
        return f"{name}|{str(event_date or '')}"

    # -- R11: the Master Pipeline snapshot ---------------------------------

    def pipeline_snapshot(self, companies: list, *, today: str) -> list:
        """Record today's company names; return the ones NEVER SEEN BEFORE.

        [{"company": name, "first_seen": iso}] for every company that was not
        already in the table, PLUS every company recorded within the last
        `NEW_COMPANY_WINDOW_DAYS` — R11 needs the recent ones too, because a
        company first seen on Friday is due on Monday and the caller must still
        be able to see it then.

        THIS IS THE WRITE THAT KEEPS THE ENGINE PURE. `nextaction` cannot take
        this snapshot itself: computing the queue would advance the state the
        queue is derived from, and every `cadence preview` would consume the
        newness it was supposed to be showing. So the caller takes it, once a
        tick, and passes the result in.

        THE FIRST RUN RECORDS EVERYTHING AND REPORTS NOTHING NEW. On an empty
        table every company looks new, and announcing 273 of them would be a
        spectacular first impression. The table is seeded silently and the rule
        starts finding genuinely new ones from the next run.
        """
        import config as _config

        seen_before = self._pipeline_known()
        first_run = not seen_before
        window = max(1, int(getattr(_config, "NEW_COMPANY_WINDOW_DAYS", 7)))

        fresh: list = []
        with self.conn() as c:
            for name in companies or []:
                label = str(name or "").strip()
                if not label:
                    continue
                key = _norm_key(label)
                if not key or key in seen_before:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO pipeline_companies "
                    "(company_key, company, first_seen, seeded) VALUES (?, ?, ?, ?)",
                    (key, label, today, 1 if first_run else 0),
                )
                seen_before.add(key)
                fresh.append(label)

        if first_run:
            log.info(
                "[db] pipeline snapshot seeded with %d company(ies) on the first run. "
                "None is reported as new — on an empty table every company looks new, "
                "and announcing the whole tab would be a poor first impression. R11 "
                "starts finding genuinely new ones from the next run.", len(fresh),
            )
            return []

        if fresh:
            log.info("[db] pipeline snapshot: %d new company(ies): %s",
                     len(fresh), ", ".join(fresh[:8]))

        # Everything recorded inside the window, so a Friday arrival is still
        # visible on Monday when its working-day delay comes due.
        cutoff = _iso_days_ago(today, window)
        with self.conn() as c:
            rows = c.execute(
                "SELECT company, first_seen FROM pipeline_companies "
                "WHERE seeded = 0 AND first_seen >= ? ORDER BY first_seen, company",
                (cutoff,),
            ).fetchall()
        return [{"company": r["company"], "first_seen": r["first_seen"]} for r in rows]

    def _pipeline_known(self) -> set:
        try:
            with self.conn() as c:
                rows = c.execute("SELECT company_key FROM pipeline_companies").fetchall()
            return {r["company_key"] for r in rows}
        except Exception:
            log.exception("[db] the pipeline snapshot could not be read")
            return set()

    # -- R5: repeat counts and the week's companies ------------------------

    def prospect_repeats(self) -> dict:
        """{row_key: count} — how many posts have carried each contact unchanged.

        FAILS OPEN, at 0. An unreadable table means the bot asks "shall I skip
        them?" later than it should, which is a mild annoyance; failing closed
        would mean it asked on the first mention, which reads as a bot giving up
        before it started.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT row_key, count FROM prospect_mentions"
                ).fetchall()
            return {r["row_key"]: int(r["count"] or 0) for r in rows}
        except Exception:
            log.exception("[db] prospect repeat counts could not be read; treating as 0")
            return {}

    def record_prospect_mention(self, row_key: str, *, signature: str, on_date: str) -> int:
        """Count one post that carried this contact. Returns the new count.

        A CHANGED SIGNATURE RESETS THE COUNT TO 1. The rule is "three times with
        nothing changed", not "three times" — somebody who updated the row has
        acted, and a counter that kept climbing through their update would ask
        to skip a contact who is actually moving.
        """
        key = str(row_key or "").strip()
        if not key:
            return 0
        with self.conn() as c:
            row = c.execute(
                "SELECT count, signature FROM prospect_mentions WHERE row_key = ?",
                (key,),
            ).fetchone()
            if row and str(row["signature"] or "") == str(signature or ""):
                count = int(row["count"] or 0) + 1
            else:
                count = 1
            c.execute(
                "INSERT INTO prospect_mentions (row_key, count, signature, last_date) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(row_key) DO UPDATE SET "
                "count = excluded.count, signature = excluded.signature, "
                "last_date = excluded.last_date, updated_at = CURRENT_TIMESTAMP",
                (key, count, str(signature or ""), str(on_date or "")),
            )
        return count

    def prospect_week_companies(self, iso_week: str) -> list:
        """The companies this week's prospecting is already working through."""
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT company FROM prospect_week WHERE iso_week = ? "
                    "ORDER BY started_on, company",
                    (str(iso_week),),
                ).fetchall()
            return [r["company"] for r in rows]
        except Exception:
            log.exception("[db] the week's prospect companies could not be read")
            return []

    def start_prospect_company(self, company: str, *, iso_week: str, on_date: str) -> None:
        """Remember that this week's prospecting has opened this company."""
        label = str(company or "").strip()
        if not label:
            return
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO prospect_week (iso_week, company, started_on) "
                "VALUES (?, ?, ?)",
                (str(iso_week), label, str(on_date or "")),
            )

    # -- R9: the meeting follow-up ladder ----------------------------------

    def meeting_followups(self) -> dict:
        """{row_key: {"sent": n, "last_iso": iso, "meeting": iso}} for R9.

        FAILS CLOSED, at "already finished". An unreadable ladder table returns
        {} and every row then reads as rung 0 — which would restart a chase
        somebody has already escalated. The empty dict is the honest answer and
        the caller treats a missing entry as rung 0 only because a row that has
        never been chased genuinely is at rung 0; the failure is logged loudly
        so a broken table is not mistaken for a fresh one.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT row_key, sent, last_date, meeting_date FROM meeting_followups"
                ).fetchall()
            return {
                r["row_key"]: {
                    "sent": int(r["sent"] or 0),
                    "last_iso": str(r["last_date"] or ""),
                    "meeting": str(r["meeting_date"] or ""),
                }
                for r in rows
            }
        except Exception:
            log.exception(
                "[db] the meeting follow-up ladder could not be read. Every row will "
                "read as rung 0, which can restart a chase that was already escalated."
            )
            return {}

    def advance_meeting_followup(self, row_key: str, *, on_date: str,
                                 meeting_date: str = "") -> int:
        """Move one meeting up a rung. Returns the new rung count.

        CALLED BY THE SENDER, NEVER BY THE ENGINE. If computing the queue
        advanced the ladder, every `cadence preview` would burn a rung and the
        preview would change what it was previewing.
        """
        key = str(row_key or "").strip()
        if not key:
            return 0
        with self.conn() as c:
            row = c.execute(
                "SELECT sent FROM meeting_followups WHERE row_key = ?", (key,)
            ).fetchone()
            sent = int((row["sent"] if row else 0) or 0) + 1
            c.execute(
                "INSERT INTO meeting_followups (row_key, sent, last_date, meeting_date) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(row_key) DO UPDATE SET "
                "sent = excluded.sent, last_date = excluded.last_date, "
                "meeting_date = excluded.meeting_date, updated_at = CURRENT_TIMESTAMP",
                (key, sent, str(on_date or ""), str(meeting_date or "")),
            )
        return sent

    def reset_meeting_followup(self, row_key: str) -> None:
        """Next steps arrived — the chase is over and the ladder is cleared.

        Without this a row that got its next steps after two DMs would stay at
        rung 2 forever, and the next stalled meeting on the same contact would
        start at the escalation.
        """
        key = str(row_key or "").strip()
        if not key:
            return
        with self.conn() as c:
            c.execute("DELETE FROM meeting_followups WHERE row_key = ?", (key,))

    # -- R13: the next-step reminders ---------------------------------------

    def next_step_state(self) -> Optional[dict]:
        """{row_key: {signature, last_date, mentions, calls, last_call_date,
        last_ask, closed, closed_date}} for R13, or None when it cannot be read.

        FAILS CLOSED, AND NOT THE WAY R9's LADDER DOES. An empty dict here would
        mean "nobody has ever been named": the rotation would start again from
        the top of the sheet and every contact already closed as Unresponsive
        would be chased again. None means "unknown", and the evaluator posts
        nothing on it.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT row_key, signature, last_date, mentions, calls, "
                    "last_call_date, last_ask, closed, closed_date "
                    "FROM next_step_followups"
                ).fetchall()
            return {
                r["row_key"]: {
                    "signature": str(r["signature"] or ""),
                    "last_date": str(r["last_date"] or ""),
                    "mentions": int(r["mentions"] or 0),
                    "calls": int(r["calls"] or 0),
                    "last_call_date": str(r["last_call_date"] or ""),
                    "last_ask": str(r["last_ask"] or ""),
                    "closed": bool(r["closed"]),
                    "closed_date": str(r["closed_date"] or ""),
                }
                for r in rows
            }
        except Exception:
            log.exception(
                "[db] the next-step reminder state could not be read. R13 posts "
                "nothing until it can: guessing would restart the rotation and "
                "chase people it has already closed."
            )
            return None

    def record_next_step_mention(self, row_key: str, *, signature: str,
                                 on_date: str, ask: str) -> None:
        """R13 named this contact in a post that really went out.

        CALLED BY THE SENDER, NEVER BY THE ENGINE, for the same reason as
        `advance_meeting_followup`: a preview that recorded a mention would
        change the rotation it was previewing.

        A different stored signature means the step changed since the last
        mention, so the counts for the old step are dropped first. `closed`
        survives that on purpose.
        """
        key = str(row_key or "").strip()
        if not key:
            return
        sig = str(signature or "")
        day = str(on_date or "")
        what = str(ask or "")
        with self.conn() as c:
            row = c.execute(
                "SELECT signature, mentions, calls, last_call_date, closed, closed_date "
                "FROM next_step_followups WHERE row_key = ?", (key,)
            ).fetchone()
            same = bool(row) and str(row["signature"] or "") == sig
            mentions = (int(row["mentions"] or 0) if same else 0) + 1
            calls = int(row["calls"] or 0) if same else 0
            last_call = str(row["last_call_date"] or "") if same else ""
            closed = int(row["closed"] or 0) if row else 0
            closed_date = str(row["closed_date"] or "") if row else ""
            if what == "call":
                calls += 1
                last_call = day
            if what == "unresponsive":
                closed, closed_date = 1, day
            c.execute(
                "INSERT INTO next_step_followups (row_key, signature, last_date, "
                "mentions, calls, last_call_date, last_ask, closed, closed_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(row_key) DO UPDATE SET "
                "signature = excluded.signature, last_date = excluded.last_date, "
                "mentions = excluded.mentions, calls = excluded.calls, "
                "last_call_date = excluded.last_call_date, last_ask = excluded.last_ask, "
                "closed = excluded.closed, closed_date = excluded.closed_date, "
                "updated_at = CURRENT_TIMESTAMP",
                (key, sig, day, mentions, calls, last_call, what, closed, closed_date),
            )

    def record_next_step_post(self, message_id, *, on_date: str, channel_id="",
                              people=None) -> None:
        """Remember who an R13 post named, and what each was asked."""
        mid = str(message_id or "").strip()
        if not mid:
            return
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO next_step_posts (message_id, on_date, "
                "channel_id, people) VALUES (?, ?, ?, ?)",
                (mid, str(on_date or ""), str(channel_id or ""),
                 json.dumps(list(people or []), ensure_ascii=False)),
            )

    def next_step_post(self, message_id) -> Optional[dict]:
        """{message_id, on_date, channel_id, people} for one R13 post, or None."""
        mid = str(message_id or "").strip()
        if not mid:
            return None
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT message_id, on_date, channel_id, people "
                    "FROM next_step_posts WHERE message_id = ?", (mid,)
                ).fetchone()
        except Exception:
            log.exception("[db] next_step_posts could not be read")
            return None
        if row is None:
            return None
        try:
            people = json.loads(row["people"] or "[]")
        except ValueError:
            people = []
        return {
            "message_id": str(row["message_id"]),
            "on_date": str(row["on_date"] or ""),
            "channel_id": str(row["channel_id"] or ""),
            "people": people if isinstance(people, list) else [],
        }

    # -- permission before every write: proposals ---------------------------

    def open_proposal(self, *, proposal_key: str, kind: str, tab: str,
                      sheet_row, row_key: str, company: str, poc: str,
                      payload: dict, reply_text: str, trigger: str,
                      proposed_text: str, requested_by: str, channel_id,
                      message_id: str, created_at: str) -> bool:
        """Record a write the bot has DESCRIBED and not made. False if it exists.

        `payload` is the whole plan, as JSON — the cells, the labels, the old
        values. `reply_text` is the ORIGINAL message, kept because the terminal-
        word gate is matched against what the human wrote and "yes" contains no
        terminal word at all.
        """
        import json as _json
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO write_proposals (proposal_key, kind, tab, sheet_row, "
                    "row_key, company, poc, payload, reply_text, trigger, "
                    "proposed_text, requested_by, channel_id, message_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (proposal_key, kind, tab, sheet_row, row_key, company, poc,
                     _json.dumps(payload or {}), reply_text or "", trigger or "",
                     proposed_text or "", requested_by or "", channel_id,
                     str(message_id or ""), created_at or ""),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_proposal_message(self, proposal_key: str, message_id: str) -> None:
        """Key an open proposal to the message that ASKED it.

        For a proposal recorded BEFORE its question is posted (the row_add
        offer: no proposal, no message) — the id only exists once the message
        has landed, and a reply to that message is how a yes finds it.
        """
        with self.conn() as c:
            c.execute(
                "UPDATE write_proposals SET message_id = ? WHERE proposal_key = ?",
                (str(message_id or ""), str(proposal_key)),
            )

    def proposal(self, proposal_key: str) -> Optional[dict]:
        """One proposal, with its votes. None when there is no such key."""
        import json as _json
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM write_proposals WHERE proposal_key = ?",
                (str(proposal_key),),
            ).fetchone()
            if row is None:
                return None
            votes = c.execute(
                "SELECT voter_id, voter_label, vote, voted_at FROM proposal_votes "
                "WHERE proposal_key = ? ORDER BY voted_at",
                (str(proposal_key),),
            ).fetchall()
        out = dict(row)
        try:
            out["payload"] = _json.loads(out.get("payload") or "{}")
        except (ValueError, TypeError):
            out["payload"] = {}
        out["votes"] = [dict(v) for v in votes]
        return out

    def open_proposal_for_message(self, message_id: str) -> Optional[dict]:
        """The open proposal whose own message is `message_id`.

        How a reply finds what it is answering: somebody replies "yes" to the
        bot's proposal, and this is the lookup that turns that reply into the
        write it approves.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE message_id = ? AND status = 'open'",
                (str(message_id or ""),),
            ).fetchone()
        return self.proposal(row["proposal_key"]) if row else None

    def open_proposals_for_message(self, message_id: str) -> list[dict]:
        """EVERY open proposal keyed to one message, oldest first.

        One post can ask more than one thing — R3 may offer to add events it
        found, to fill in deadlines, and to remind the team again — and each is
        its own proposal. The caller picks which one a reply answers.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE message_id = ? AND status = 'open' ORDER BY created_at, rowid",
                (str(message_id or ""),),
            ).fetchall()
        out = [self.proposal(r["proposal_key"]) for r in rows]
        return [p for p in out if p]

    def open_proposals_of_kind(self, kind: str) -> list[dict]:
        """Every open proposal of one kind, oldest first.

        So a question already waiting on a yes is not asked a second time: the
        add offer reads the open `row_add` proposals before it names anybody.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE kind = ? AND status = 'open' ORDER BY id",
                (str(kind or ""),),
            ).fetchall()
        out = [self.proposal(r["proposal_key"]) for r in rows]
        return [p for p in out if p]

    def proposals_for_message(self, message_id: str) -> list[dict]:
        """EVERY proposal keyed to one message, open or not, oldest first.

        So a second "yes" under an offer that was already answered (or that
        lapsed) can be told so, by name, instead of being read as a fresh
        request. `open_proposals_for_message` is the one a vote uses.
        """
        mid = str(message_id or "").strip()
        if not mid:
            return []
        with self.conn() as c:
            rows = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE message_id = ? ORDER BY created_at, rowid", (mid,),
            ).fetchall()
        out = [self.proposal(r["proposal_key"]) for r in rows]
        return [p for p in out if p]

    def open_proposals_in_channel(self, channel_id) -> list[dict]:
        """Every open proposal made in one channel, oldest first.

        FOR A BARE "YES" THAT IS NOT A REPLY, and only for that. The channel is
        the widest a yes may reach: a proposal made in another sales channel is
        not what somebody typing here is answering. The caller applies the age
        window and asks "which one?" when this returns more than one.
        """
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return []
        with self.conn() as c:
            rows = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE status = 'open' AND CAST(channel_id AS INTEGER) = ? "
                "ORDER BY id", (cid,),
            ).fetchall()
        out = [self.proposal(r["proposal_key"]) for r in rows]
        return [p for p in out if p]

    def open_nudged_proposals(self) -> list[dict]:
        """Every open proposal that has had its one nudge, oldest first.

        WHAT THE "WAITING FOR YOUR YES" POST LISTED. That post is one message
        for several proposals and none of them is keyed to it, so a "yes"
        replied to it finds them here: a proposal is nudged once and dropped at
        the next sweep, so the open-and-nudged ones are exactly the last
        list's. From the table, not from memory, so it survives a restart.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT proposal_key FROM write_proposals "
                "WHERE status = 'open' AND nudged_on != '' ORDER BY id",
            ).fetchall()
        out = [self.proposal(r["proposal_key"]) for r in rows]
        return [p for p in out if p]

    def latest_open_proposal(self, *, company: str = "") -> Optional[dict]:
        """The newest open proposal, optionally for one company.

        NOTHING ANSWERS A MESSAGE WITH THIS SINCE NFT2-1063. It was the lookup
        for a "yes" that named no proposal, and it has no channel and no age:
        on 7 Oct a "sure" under an unrelated line reached an offer made hours
        earlier and scheduled a reminder nobody asked for. A reply now votes
        only on the proposals of the message it replies to, and a bare yes goes
        through `open_proposals_in_channel` and an age window. Kept for
        operator and test use; do not put it back on the reply path.
        """
        sql = "SELECT proposal_key FROM write_proposals WHERE status = 'open'"
        args: list = []
        if company:
            sql += " AND LOWER(company) = LOWER(?)"
            args.append(company)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.conn() as c:
            row = c.execute(sql, args).fetchone()
        return self.proposal(row["proposal_key"]) if row else None

    def record_vote(self, *, proposal_key: str, voter_id: int, voter_label: str,
                    vote: str, voted_at: str, message_id: str = "") -> None:
        """One approver's answer. A later answer from the same person REPLACES
        their earlier one — somebody who says "no, wait" after a yes has changed
        their mind, and the bot should act on what they think now."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO proposal_votes (proposal_key, voter_id, voter_label, "
                "vote, voted_at, message_id) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(proposal_key, voter_id) DO UPDATE SET "
                "vote = excluded.vote, voted_at = excluded.voted_at, "
                "message_id = excluded.message_id",
                (str(proposal_key), int(voter_id), voter_label or "", str(vote),
                 voted_at or "", str(message_id or "")),
            )

    def close_proposal(self, *, proposal_key: str, status: str, decision: str,
                       decided_by: str, decided_at: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE write_proposals SET status = ?, decision = ?, decided_by = ?, "
                "decided_at = ? WHERE proposal_key = ?",
                (status, decision, decided_by or "", decided_at or "",
                 str(proposal_key)),
            )

    def stale_proposals(self, *, before_iso: str, nudged: bool) -> list:
        """Open proposals created on or before `before_iso` (an IST date),
        split by whether they have already had their one nudge.

        `nudged=False` -> due a nudge. `nudged=True` -> due to be dropped.

        THE DATE COMPARISON HAPPENS IN PYTHON, NOT IN SQL, and that is
        deliberate. Two bugs live at this boundary and only one of them is
        obvious:

          1. `created_at` is a full ISO TIMESTAMP and `before_iso` is a bare
             DATE. Compared as strings the timestamp is the LONGER value, so
             "2026-09-18T14:00:00" <= "2026-09-18" is FALSE — a proposal created
             on the cutoff day never qualified and the sweep silently found
             nothing, for ever.

          2. `substr(created_at, 1, 10)` fixes (1) by reading the first ten
             characters and calling them the date. That is right only while
             every writer happens to store IST — today's convention
             (`dl.now_ist()`), but enforced nowhere. A single caller using
             `datetime.now(timezone.utc)` would shift the comparison by 5.5
             hours: a proposal made at 00:30 IST is 19:00 UTC THE PREVIOUS DAY,
             and SQL would read it as yesterday's.

        `dl.ist_date_of` parses whatever offset the row carries and converts to
        IST before taking the date, so both shapes are right. The cost is
        fetching the open rows and filtering here — which is nothing, because
        open proposals are nudged and dropped within days by construction and
        there are never many.
        """
        import deadlines as _dl

        cutoff = _dl.ist_date_of(before_iso)
        if cutoff is None:
            log.warning(
                "[approvals] stale_proposals was given an unreadable cutoff (%r); "
                "returning nothing rather than sweeping an unknown range.",
                before_iso,
            )
            return []

        sql = (
            "SELECT proposal_key, created_at FROM write_proposals "
            "WHERE status = 'open' AND nudged_on "
            + ("!= ''" if nudged else "= ''")
            + " ORDER BY id"
        )
        with self.conn() as c:
            rows = c.execute(sql).fetchall()

        out = []
        for row in rows:
            made = _dl.ist_date_of(row["created_at"])
            if made is None:
                log.warning(
                    "[approvals] proposal %s has an unreadable created_at (%r); it is "
                    "skipped by the sweep rather than dropped on a guess.",
                    row["proposal_key"], row["created_at"],
                )
                continue
            if made <= cutoff:
                out.append(self.proposal(row["proposal_key"]))
        return out

    def mark_proposal_nudged(self, proposal_key: str, *, on_date: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE write_proposals SET nudged_on = ? WHERE proposal_key = ?",
                (on_date or "", str(proposal_key)),
            )

    # -- focus ---------------------------------------------------------------

    def set_focus(self, *, field: str, value: str, raw: str, set_by: str,
                  set_by_id, set_on: str, expires_on: str) -> int:
        """Start a focus, clearing any that is still live.

        ONE FOCUS AT A TIME, deliberately. Two overlapping focuses is a filter
        nobody can predict the effect of, and "prioritise X" said twice means
        the second one — not both at once.
        """
        with self.conn() as c:
            c.execute(
                "UPDATE focus SET cleared_on = ?, cleared_by = ? "
                "WHERE cleared_on = '' AND expires_on > ?",
                (set_on, "superseded by a new focus", set_on),
            )
            cur = c.execute(
                "INSERT INTO focus (field, value, raw, set_by, set_by_id, set_on, "
                "expires_on) VALUES (?,?,?,?,?,?,?)",
                (field or "", value, raw or "", set_by or "", set_by_id, set_on,
                 expires_on),
            )
            return int(cur.lastrowid or 0)

    def active_focus(self, *, today: str) -> Optional[dict]:
        """The focus in force today, or None. Expiry is exclusive of the day it
        names: "for two weeks" ends at the end of the fourteenth day."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM focus WHERE cleared_on = '' AND expires_on >= ? "
                "ORDER BY id DESC LIMIT 1",
                (str(today),),
            ).fetchone()
        return dict(row) if row else None

    def clear_focus(self, *, on_date: str, by: str) -> Optional[dict]:
        """End the live focus. Returns what was cleared, or None."""
        live = self.active_focus(today=on_date)
        if not live:
            return None
        with self.conn() as c:
            c.execute(
                "UPDATE focus SET cleared_on = ?, cleared_by = ? WHERE id = ?",
                (on_date, by or "", int(live["id"])),
            )
        return live

    def focus_due_expiry_announcement(self, *, today: str) -> Optional[dict]:
        """A focus that has just run out and has not been announced. Once only.

        The announcement is a courtesy — the team should know the filter came
        off — and it is exactly once, because a bot that mentions a lapsed focus
        every morning is a bot with a stuck record.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM focus WHERE announced_expiry = 0 AND cleared_on = '' "
                "AND expires_on < ? ORDER BY id DESC LIMIT 1",
                (str(today),),
            ).fetchone()
        return dict(row) if row else None

    def mark_focus_announced(self, focus_id: int) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE focus SET announced_expiry = 1 WHERE id = ?", (int(focus_id),)
            )

    # -- rule 9's answers, which the bot may not put in the sheet -----------

    def record_meeting_outcome(self, *, row_key: str, company: str, poc: str,
                               next_steps: str = "", package: str = "",
                               deal_size: str = "", told_by: str = "",
                               told_at: str = "", message_id: str = "") -> int:
        """Record what somebody said came out of a meeting.

        THIS IS NOT THE SHEET AND IS NEVER PRESENTED AS IT. Next steps, package
        and deal size live in Z-AE, the restricted commercial block, and the bot
        does not write there — on any row, with any approval. What this table is
        for is that the answer is not LOST between being given in a channel and
        being typed into the sheet by a person. `in_sheet` stays 0 until
        somebody says it is in.
        """
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO meeting_outcomes (row_key, company, poc, next_steps, "
                "package, deal_size, told_by, told_at, message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row_key or "", company or "", poc or "", next_steps or "",
                 package or "", deal_size or "", told_by or "", told_at or "",
                 str(message_id or "")),
            )
            return int(cur.lastrowid or 0)

    def meeting_outcomes_not_in_sheet(self, limit: int = 20) -> list:
        """What people have told the bot that nobody has typed in yet."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM meeting_outcomes WHERE in_sheet = 0 "
                "ORDER BY id DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- the daily web-search budget ---------------------------------------

    def web_searches_today(self, on_date: str) -> int:
        """How many searches have been BILLED today, across every rule.

        FAILS CLOSED, at the budget. An unreadable ledger returns a number that
        stops further searching rather than one that permits it: the failure
        mode of over-reporting is a quiet day the bot explains, and the failure
        mode of under-reporting is an unbounded bill nobody sees until it
        arrives.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT COALESCE(SUM(searches), 0) AS n FROM web_search_usage "
                    "WHERE on_date = ?", (str(on_date),),
                ).fetchone()
            return int((row["n"] if row else 0) or 0)
        except Exception:
            log.exception(
                "[websearch] the daily ledger could not be read; treating the budget "
                "as SPENT. Rules will say web research is unavailable today rather "
                "than searching an unknown number of times."
            )
            import config as _config
            return _config.search_daily_budget()

    def web_search_budget_left(self, on_date: str) -> int:
        """Requests left today — SEARCH_DAILY_BUDGET for the backends that
        search outside the model, WEB_SEARCH_DAILY_BUDGET under anthropic."""
        import config as _config
        budget = _config.search_daily_budget()
        return max(0, budget - self.web_searches_today(on_date))

    def record_web_search(self, *, on_date: str, rule_id: str, searches: int,
                          errors: int = 0, backend: str = "") -> int:
        """Bank one call's billed searches. Returns the new day total.

        Called AFTER the response comes back, with the count the API reported —
        which is why a call that errored adds 0 to `searches` and 1 to `errors`.
        Reserving before the call would spend a budget on searches that never
        happened.

        `backend` (searxng | ddg | google_cse | anthropic) is stored beside
        the count so "what did you cost" can price each request.
        """
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO web_search_usage (on_date, rule_id, searches, calls, "
                    "errors, backend) VALUES (?,?,?,1,?,?) "
                    "ON CONFLICT(on_date, rule_id) DO UPDATE SET "
                    "searches = searches + excluded.searches, "
                    "calls = calls + 1, errors = errors + excluded.errors, "
                    "backend = CASE WHEN excluded.backend <> '' THEN excluded.backend "
                    "          ELSE backend END, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (str(on_date), str(rule_id or ""), max(0, int(searches or 0)),
                     max(0, int(errors or 0)), str(backend or "")),
                )
        except Exception:
            log.exception("[websearch] could not record %d search(es) for %s",
                          searches, rule_id)
        return self.web_searches_today(on_date)

    def web_search_breakdown(self, on_date: str) -> list:
        """[{rule_id, searches, calls, errors}] for one day — what spent it."""
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT rule_id, searches, calls, errors, backend "
                    "FROM web_search_usage "
                    "WHERE on_date = ? ORDER BY searches DESC", (str(on_date),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            log.exception("[websearch] the ledger breakdown could not be read")
            return []

    def web_searches_between(self, start_iso: str, end_iso: str) -> list:
        """[{rule_id, searches, calls}] summed over a date range, inclusive."""
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT rule_id, SUM(searches) AS searches, SUM(calls) AS calls, "
                    "MAX(backend) AS backend "
                    "FROM web_search_usage WHERE on_date BETWEEN ? AND ? "
                    "GROUP BY rule_id, backend ORDER BY searches DESC",
                    (str(start_iso), str(end_iso)),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            log.exception("[websearch] the ledger range could not be read")
            return []

    # -- a backend's own daily quota (search_backend.py) --------------------

    def search_quota_used(self, on_date: str, backend: str) -> int:
        """Calls made to `backend` on `on_date` (the provider's day).

        FAILS CLOSED, like the request budget: an unreadable count returns a
        number no free quota exceeds, so the backend is skipped rather than
        billed. The next backend in SEARCH_FALLBACKS still answers.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT requests FROM search_quota_usage "
                    "WHERE on_date = ? AND backend = ?",
                    (str(on_date), str(backend)),
                ).fetchone()
            return int((row["requests"] if row else 0) or 0)
        except Exception:
            log.exception("[search] the %s quota could not be read; treating it "
                          "as SPENT", backend)
            return 10 ** 9

    def search_quota_add(self, on_date: str, backend: str, n: int = 1) -> None:
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO search_quota_usage (on_date, backend, requests) "
                    "VALUES (?,?,?) ON CONFLICT(on_date, backend) DO UPDATE SET "
                    "requests = requests + excluded.requests, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (str(on_date), str(backend), max(0, int(n or 0))),
                )
        except Exception:
            log.exception("[search] could not count a %s request", backend)

    # -- the token log ------------------------------------------------------

    def record_llm_call(self, row: dict) -> None:
        """One Anthropic call. Never raises — a lost row must not cost a reply."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO llm_calls (ts, site, model, input_tokens, cache_write, "
                    "cache_read, output_tokens, seconds, ok) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(row.get("ts")), str(row.get("site")), str(row.get("model") or ""),
                     int(row.get("input_tokens") or 0), int(row.get("cache_write") or 0),
                     int(row.get("cache_read") or 0), int(row.get("output_tokens") or 0),
                     float(row.get("seconds") or 0), 1 if row.get("ok", True) else 0),
                )
        except Exception:
            log.exception("[tokens] could not record an API call")

    def llm_usage_by_site(self, since_ts: str) -> list[dict]:
        """[{site, calls, input_tokens, cache_write, cache_read, output_tokens}]
        since `since_ts`, the biggest spender first."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT site, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                "SUM(cache_write) AS cache_write, SUM(cache_read) AS cache_read, "
                "SUM(output_tokens) AS output_tokens FROM llm_calls WHERE ts >= ? "
                "GROUP BY site ORDER BY SUM(input_tokens + cache_write + cache_read "
                "+ output_tokens) DESC", (str(since_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def llm_calls_since(self, since_ts: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM llm_calls WHERE ts >= ? ORDER BY ts, rowid",
                             (str(since_ts),)).fetchall()
        return [dict(r) for r in rows]

    def llm_usage_by_site_model(self, since_ts: str) -> list[dict]:
        """[{site, model, calls, input_tokens, cache_write, cache_read,
        output_tokens}] since `since_ts` — the grain the dollar figures need,
        because a token's price depends on the model that read it."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT site, model, COUNT(*) AS calls, "
                "SUM(input_tokens) AS input_tokens, SUM(cache_write) AS cache_write, "
                "SUM(cache_read) AS cache_read, SUM(output_tokens) AS output_tokens "
                "FROM llm_calls WHERE ts >= ? GROUP BY site, model", (str(since_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def llm_budget_tokens_since(self, since_ts: str) -> int:
        """Input tokens spent since `since_ts`, as TOKEN_DAILY_BUDGET counts
        them: uncached input and cache writes in full, cache reads at 10%.

        FAILS OPEN, at zero. An unreadable token log must not be the reason a
        person's question goes unanswered; the search-request budget (which
        fails closed) still bounds the day.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT COALESCE(SUM(input_tokens + cache_write), 0) AS full, "
                    "COALESCE(SUM(cache_read), 0) AS reads FROM llm_calls "
                    "WHERE ts >= ?", (str(since_ts),),
                ).fetchone()
            return int(row["full"] or 0) + int(round(int(row["reads"] or 0) * 0.1))
        except Exception:
            log.exception("[tokens] the token log could not be read for the budget")
            return 0

    # -- the search cache (search_backend.py) -------------------------------

    def search_cache_get(self, cache_key: str, *, since_utc: str) -> Optional[dict]:
        """The cached row for this key fetched at or after `since_utc`, or None.
        FAILS OPEN, to a miss — the budget still bounds what a miss costs."""
        import json
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT backend, query, results_json, fetched_at FROM search_cache "
                    "WHERE cache_key = ? AND fetched_at >= ?",
                    (str(cache_key), str(since_utc)),
                ).fetchone()
            if row is None:
                return None
            return {"backend": row["backend"], "query": row["query"],
                    "results": json.loads(row["results_json"] or "[]"),
                    "fetched_at": row["fetched_at"]}
        except Exception:
            log.exception("[search] the cache could not be read; treating as a miss")
            return None

    def search_cache_put(self, cache_key: str, *, backend: str, query: str,
                         results, fetched_at: str) -> bool:
        import json
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO search_cache (cache_key, backend, query, results_json, "
                    "fetched_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET backend = excluded.backend, "
                    "query = excluded.query, results_json = excluded.results_json, "
                    "fetched_at = excluded.fetched_at",
                    (str(cache_key), str(backend or ""), str(query or "")[:500],
                     json.dumps(results, ensure_ascii=False), str(fetched_at)),
                )
            return True
        except Exception:
            log.exception("[search] could not store a cache row")
            return False

    # -- R1: the feed store (feeds.py) --------------------------------------

    def news_feed_add(self, items: list) -> int:
        """Store the feed items that are NEW. Returns how many were.

        NEW MEANS NEITHER KEY IS KNOWN: the same link (url_key) or the same
        headline from another outlet (headline_key) is the same story and is
        not stored twice.

        A STORY ALREADY STORED AS INDUSTRY NEWS THAT A PoC QUERY ALSO RETURNS
        BECOMES A PoC STORY. The outlets' feeds are read first, so a funding
        round at a pipeline company usually arrives from TechCrunch before the
        company's own query finds it; without this it would stay "industry" and
        lose both its sheet row and its place in the PoC slots. Its score is
        kept — it is the same story.
        """
        added = 0
        with self.conn() as c:
            for it in items or []:
                ukey = str(it.get("url_key") or "").strip()
                hkey = str(it.get("headline_key") or "").strip()
                if not ukey:
                    continue
                kind = "poc" if str(it.get("kind") or "") == "poc" else "industry"
                ref = str(it.get("sheet_ref") or "")[:200]
                seen = c.execute(
                    "SELECT url_key, kind FROM news_feed_items WHERE url_key = ? "
                    "OR (? <> '' AND headline_key = ?) LIMIT 1", (ukey, hkey, hkey),
                ).fetchone()
                if seen is not None:
                    if kind == "poc" and str(seen["kind"] or "") != "poc":
                        c.execute(
                            "UPDATE news_feed_items SET kind = 'poc', sheet_ref = ? "
                            "WHERE url_key = ?", (ref, seen["url_key"]))
                    continue
                c.execute(
                    "INSERT INTO news_feed_items (url_key, headline_key, url, title, "
                    "summary, source, published_at, topic_hint, seen_at, kind, "
                    "sheet_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ukey, hkey, str(it.get("url") or ""), str(it.get("title") or "")[:300],
                     str(it.get("summary") or "")[:300], str(it.get("source") or "")[:120],
                     str(it.get("published_at") or ""), str(it.get("topic_hint") or ""),
                     str(it.get("seen_at") or ""), kind, ref),
                )
                added += 1
        return added

    def news_feed_between(self, since_utc: str, until_utc: str) -> list[dict]:
        """Every stored item that belongs to [since, until], newest first.

        PUBLISHED in the window — or, for a PoC item, FIRST SEEN in it. A name
        is looked up every few weeks and its query reaches back
        NEWS_POC_LOOKBACK_DAYS, so a PoC story is often a few days old on the
        day the bot first learns of it; by its publication date alone it would
        belong to a window that closed before anybody had looked.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM news_feed_items WHERE "
                "(published_at >= ? AND published_at <= ?) "
                "OR (kind = 'poc' AND seen_at >= ? AND seen_at <= ?) "
                "ORDER BY published_at DESC",
                (str(since_utc), str(until_utc), str(since_utc), str(until_utc)),
            ).fetchall()
        return [dict(r) for r in rows]

    def news_feed_set_scores(self, scored: list, *, scored_at: str) -> int:
        """Write the light model's verdicts back: [{url_key, importance, topic,
        what}]. An item scored once is never paid for again."""
        rows = [(max(1, min(5, int(s.get("importance") or 1))),
                 str(s.get("topic") or ""), str(s.get("what") or "")[:300],
                 str(scored_at), str(s["url_key"]))
                for s in (scored or []) if str(s.get("url_key") or "").strip()]
        if not rows:
            return 0
        with self.conn() as c:
            c.executemany(
                "UPDATE news_feed_items SET importance = ?, topic = ?, what = ?, "
                "scored_at = ? WHERE url_key = ?", rows)
        return len(rows)

    def news_feed_prune(self, before_utc: str) -> int:
        with self.conn() as c:
            return int(c.execute(
                "DELETE FROM news_feed_items WHERE published_at < ? AND seen_at < ?",
                (str(before_utc), str(before_utc))).rowcount or 0)

    def news_feed_count(self) -> int:
        with self.conn() as c:
            return int(c.execute(
                "SELECT COUNT(*) AS n FROM news_feed_items").fetchone()["n"] or 0)

    # -- "reset test state" / "start over" ----------------------------------

    def wipe_operational(self, keep=KEPT_ON_RESET) -> dict:
        """Empty every table EXCEPT `keep`. {"wiped": [...], "kept": {name: rows}}.

        THE CACHES AND THE COST LEDGERS SURVIVE. A reset is for the state a
        test day builds up — sends, proposals, snoozes, reminders, posted
        stories, the pretend clock — not for answers that were paid for. The
        schema is untouched; only rows go.
        """
        kept = {str(k) for k in (keep or ())}
        wiped: list = []
        counts: dict = {}
        with self.conn() as c:
            names = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()]
            for name in names:
                if name in kept:
                    counts[name] = int(c.execute(
                        f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"] or 0)
                    continue
                c.execute(f"DELETE FROM {name}")
                wiped.append(name)
        return {"wiped": sorted(wiped), "kept": counts}

    # -- how long answers take ---------------------------------------------

    def record_reply_latency(self, *, ts: str, route: str, seconds: float,
                             used_web: bool = False, tool_calls: int = 0,
                             interim_sent: bool = False) -> None:
        """One row per answered question. Never raises — a lost timing row must
        not cost somebody their answer."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO reply_latency (ts, route, seconds, used_web, "
                    "tool_calls, interim_sent) VALUES (?,?,?,?,?,?)",
                    (str(ts), str(route), round(float(seconds), 3),
                     1 if used_web else 0, max(0, int(tool_calls or 0)),
                     1 if interim_sent else 0),
                )
        except Exception:
            log.exception("[latency] could not record a reply timing")

    def reply_latency_since(self, since_ts: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM reply_latency WHERE ts >= ? ORDER BY ts",
                (str(since_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def reply_latency_summary(self, since_ts: str) -> dict:
        """{"routes": {route: {n, p50, p90}}, "interims": int, "total": int}.

        Nearest-rank percentiles: with a handful of rows an interpolated p90 is
        a number that never happened, and this is read by a person deciding a
        threshold.
        """
        rows = self.reply_latency_since(since_ts)
        by_route: dict = {}
        for r in rows:
            by_route.setdefault(r["route"], []).append(float(r["seconds"]))

        def rank(vals, pct):
            vals = sorted(vals)
            k = max(1, -(-len(vals) * pct // 100))   # ceil
            return vals[int(k) - 1]

        return {
            "routes": {route: {"n": len(v), "p50": rank(v, 50), "p90": rank(v, 90)}
                       for route, v in sorted(by_route.items())},
            "interims": sum(1 for r in rows if r["interim_sent"]),
            "total": len(rows),
        }

    # -- the opening-variety ledger ----------------------------------------

    def recent_openers(self, limit: int = 5) -> list:
        """The last `limit` openings, newest first.

        FAILS OPEN, at []. An unreadable ledger means the composer is not told
        what to avoid and may repeat an opening — mildly annoying. Failing the
        other way would mean refusing to compose at all, which costs the whole
        message to save a turn of phrase.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT opener FROM message_openers ORDER BY id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            return [r["opener"] for r in rows if r["opener"]]
        except Exception:
            log.exception(
                "[tone] the opener ledger could not be read; the composer will not "
                "be told what to avoid this time"
            )
            return []

    def record_opener(self, opener: str, *, rule_id: str = "",
                      sent_at: str = "", keep: int = 50) -> None:
        """Remember one opening, and trim the table.

        TRIMMED RATHER THAN ALLOWED TO GROW. Only the last few are ever read, so
        a table with ten thousand rows in it is ten thousand rows of nothing.
        """
        key = str(opener or "").strip()
        if not key:
            return
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO message_openers (opener, rule_id, sent_at) "
                    "VALUES (?,?,?)", (key, str(rule_id or ""), str(sent_at or "")),
                )
                c.execute(
                    "DELETE FROM message_openers WHERE id NOT IN ("
                    "SELECT id FROM message_openers ORDER BY id DESC LIMIT ?)",
                    (max(10, int(keep)),),
                )
        except Exception:
            log.exception("[tone] could not record the opener %r", key[:40])

    def opener_used_recently(self, opener: str, *, within: int = 5) -> bool:
        """Has this opening been used in the last `within` messages?

        The check the composer's output is held to after the fact — the prompt
        asks it not to repeat, and this is what notices when it did anyway.
        """
        key = str(opener or "").strip()
        if not key:
            return False
        return key in set(self.recent_openers(within))

    def event_reminder_sent(self, event_key: str) -> bool:
        """Has this event already had its one reminder?

        FAILS CLOSED — a database error returns True, meaning "assume it went".
        This is a once-forever reminder, so the cost of the two directions is
        asymmetric: losing one is a conference nobody was reminded about, and
        sending a duplicate is the bot doing the exact thing this table exists
        to prevent. On an error the bot stays quiet and logs loudly.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM event_reminders WHERE event_key = ?",
                    (str(event_key),),
                ).fetchone()
        except Exception:
            log.exception(
                "[db] could not check the event reminder log for %r — assuming it "
                "already went, rather than risking a duplicate", event_key,
            )
            return True
        return bool(row)

    def event_keys_recorded(self, prefix: str = "") -> set:
        """Every key in `event_reminders` starting with `prefix`. FAILS CLOSED:
        unreadable means "nothing has been listed", which for R3's once-only
        "date unclear" line costs one repeat rather than hiding a row for ever.
        """
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT event_key FROM event_reminders WHERE event_key LIKE ?",
                    (str(prefix) + "%",)).fetchall()
            return {str(r["event_key"]) for r in rows}
        except Exception:
            log.exception("[db] could not read the event log; treating it as empty")
            return set()

    def record_event_reminder(
        self, *, event_key: str, event: str, event_date: str,
        location: str = "", sent_on: str = "",
    ) -> bool:
        """Record that an event's one reminder has gone. False if it already had."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO event_reminders (event_key, event, event_date, "
                    "location, sent_on) VALUES (?, ?, ?, ?, ?)",
                    (str(event_key), str(event), str(event_date),
                     str(location or ""), str(sent_on or "")),
                )
        except sqlite3.IntegrityError:
            return False
        log.info("[db] event reminder recorded: %s (%s)", event, event_date)
        return True

    def list_event_reminders(self, limit: int = 100) -> list[dict]:
        """Every event already reminded about, newest event first."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT event, event_date, location, sent_on FROM event_reminders "
                "ORDER BY event_date DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_conversion(
        self, *, on_date: str, group_key: str = "", action_type: str,
        company: str = "", owner_label: str = "", source: str,
        evidence: str, citation: str = "", offered: str = "",
    ) -> int:
        """Log one nudge turned into a record-offer, with its evidence."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO nudge_conversions (on_date, group_key, action_type, "
                "company, owner_label, source, evidence, citation, offered) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(on_date), str(group_key or ""), str(action_type),
                 str(company or ""), str(owner_label or ""), str(source),
                 str(evidence)[:1000], str(citation or ""), str(offered or "")),
            )
        log.info(
            "[convert] %s x %s -> record-offer (%s: %s)",
            action_type, company or owner_label or "?", source, str(evidence)[:120],
        )
        return int(cur.lastrowid)

    def list_conversions(self, limit: int = 50) -> list[dict]:
        """Recent conversions, newest first. For "why did you offer that?"."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT on_date, action_type, company, owner_label, source, "
                "evidence, citation FROM nudge_conversions ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_drip_offer(self, *, on_date: str, slot: int, offer: str) -> bool:
        """Attach the pending record-offer to a drip slot.

        Stored so a reply of "yes" has something concrete to apply — without it
        the extractor would have to invent what "yes" meant, which is the one
        thing it must never do about a write.
        """
        with self.conn() as c:
            cur = c.execute(
                "UPDATE drip_sends SET offer = ? WHERE on_date = ? AND slot = ?",
                (str(offer or ""), str(on_date), int(slot)),
            )
        return bool(cur.rowcount)

    # -- R1: stories already posted ----------------------------------------
    #
    # `news_targets` is the PoC news rotation (S2): who is looked up, and
    # whose turn it is. Three methods; `feeds.poll` and the bot's sync use them.

    def news_targets_sync(self, targets: list) -> dict:
        """Make the rotation match the sheet. {"total", "added", "removed"}.

        `targets` is [{"key", "kind", "name", "company", "source", "sheet_row"}]
        — every name that may be looked up TODAY. A name no longer on the sheet
        (the row went inactive, the person is on the departures list now) is
        DELETED, not left to take a turn: the rule is "never anyone on the
        departures list", and a stale row would break it quietly. A name that
        is still there keeps its `last_searched`, so a sync never restarts the
        rotation.
        """
        wanted = {str(t.get("key") or "").strip(): t for t in (targets or [])
                  if str(t.get("key") or "").strip()}
        with self.conn() as c:
            have = {r["target_key"] for r in
                    c.execute("SELECT target_key FROM news_targets").fetchall()}
            gone = [k for k in have if k not in wanted]
            c.executemany("DELETE FROM news_targets WHERE target_key = ?",
                          [(k,) for k in gone])
            for key, t in wanted.items():
                c.execute(
                    "INSERT INTO news_targets (target_key, kind, name, company, "
                    "source, sheet_row) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(target_key) DO UPDATE SET kind = excluded.kind, "
                    "  name = excluded.name, company = excluded.company, "
                    "  source = excluded.source, sheet_row = excluded.sheet_row",
                    (key, str(t.get("kind") or "company"), str(t.get("name") or ""),
                     str(t.get("company") or ""), str(t.get("source") or ""),
                     t.get("sheet_row")),
                )
        return {"total": len(wanted), "added": len([k for k in wanted if k not in have]),
                "removed": len(gone)}

    def news_targets_for_day(self, on_date: str, limit: int) -> list[dict]:
        """THE DAY'S NAMES: at most `limit`, least recently checked first.

        THE SAME SET ALL DAY. A name already stamped with `on_date` is in
        today's set; only the remainder up to `limit` is drawn — never-checked
        names first ('' sorts before every date), then the oldest — and stamped.
        So the first poll of a day chooses, every later poll and every restart
        reads the same choice, and tomorrow moves on. [] when `limit` is 0.

        AMONG NAMES CHECKED EQUALLY LONG AGO THE ORDER IS A FIXED SHUFFLE (a
        hash of the key), not the alphabet: drawn alphabetically, the first
        fortnight was every company from A to F and not one person, because
        "company" sorts before "poc".
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        cols = ("target_key, kind, name, company, source, sheet_row, last_searched, "
                "searches, hits")
        try:
            with self.conn() as c:
                today = c.execute(
                    f"SELECT {cols} FROM news_targets WHERE last_searched = ? "
                    "ORDER BY target_key LIMIT ?", (str(on_date), limit)).fetchall()
                room = limit - len(today)
                fresh = []
                if room > 0:
                    waiting = c.execute(
                        f"SELECT {cols} FROM news_targets WHERE last_searched < ?",
                        (str(on_date),)).fetchall()
                    fresh = sorted(waiting, key=lambda r: (
                        str(r["last_searched"] or ""),
                        hashlib.sha1(str(r["target_key"]).encode("utf-8")).hexdigest(),
                    ))[:room]
                    c.executemany(
                        "UPDATE news_targets SET last_searched = ?, "
                        "searches = searches + 1 WHERE target_key = ?",
                        [(str(on_date), r["target_key"]) for r in fresh])
            return [dict(r) for r in list(today) + list(fresh)]
        except Exception:
            log.exception("[news] the PoC rotation could not be read; no PoC names "
                          "are looked up this poll")
            return []

    def news_target_hits(self, keys: list) -> None:
        """Count one run that found something, for each name in `keys`."""
        rows = [(str(k),) for k in (keys or []) if str(k or "").strip()]
        if not rows:
            return
        with self.conn() as c:
            c.executemany("UPDATE news_targets SET hits = hits + 1 "
                          "WHERE target_key = ?", rows)

    def news_targets_count(self) -> int:
        with self.conn() as c:
            return int(c.execute(
                "SELECT COUNT(*) AS n FROM news_targets").fetchone()["n"] or 0)

    def news_story_seen(self, url_key: str, headline_key: str = "", *,
                        since_iso: str) -> Optional[dict]:
        """The posted row matching this link OR this headline since `since_iso`,
        or None. FAILS CLOSED.

        An unreadable table reports a match and the story is skipped. The cost
        of failing this way is one story missed; the cost of the other way is
        the same story posted every day until somebody notices, which is the
        failure this table exists to prevent.
        """
        ukey = str(url_key or "").strip()
        hkey = str(headline_key or "").strip()
        if not ukey and not hkey:
            return None
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT url_key, headline_key, posted_on, kind FROM news_stories "
                    "WHERE posted_on >= ? AND ((? <> '' AND url_key = ?) "
                    "  OR (? <> '' AND headline_key = ?)) "
                    "ORDER BY posted_on DESC LIMIT 1",
                    (str(since_iso), ukey, ukey, hkey, hkey),
                ).fetchone()
            return dict(row) if row is not None else None
        except Exception:
            log.exception("[news] could not check whether a story was already posted; "
                          "treating it as seen")
            return {"url_key": ukey, "headline_key": hkey, "posted_on": "?",
                    "kind": "unreadable"}

    def record_news_stories(self, stories: list, *, on_date: str,
                            rule_id: str = "", kind: str = "main") -> int:
        """Remember what went out. Returns how many rows were written.

        AN UPSERT ON THE LINK, so a story re-posted after NEWS_REPEAT_DAYS moves
        its date forward and is remembered for another window — INSERT OR
        IGNORE would keep the old date and let it repeat daily from then on.
        """
        rows = [
            (str(s["url_key"]), str(s.get("url") or ""),
             str(s.get("headline") or s.get("title") or "")[:300],
             str(s.get("what") or s.get("about") or "")[:300], str(rule_id),
             str(kind), str(on_date), str(s.get("topic") or ""),
             str(s.get("headline_key") or ""), int(s.get("importance") or 3), str(kind),
             "poc" if str(s.get("kind") or "") == "poc" else "industry",
             str(s.get("sheet_ref") or "")[:200])
            for s in (stories or []) if str(s.get("url_key") or "").strip()
        ]
        if not rows:
            return 0
        with self.conn() as c:
            c.executemany(
                "INSERT INTO news_stories "
                "(url_key, url, title, about, rule_id, mode, posted_on, topic, "
                " headline_key, importance, kind, news_kind, sheet_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(url_key) DO UPDATE SET "
                "  url = excluded.url, title = excluded.title, about = excluded.about, "
                "  rule_id = excluded.rule_id, mode = excluded.mode, "
                "  posted_on = excluded.posted_on, topic = excluded.topic, "
                "  headline_key = excluded.headline_key, "
                "  importance = excluded.importance, kind = excluded.kind, "
                "  news_kind = excluded.news_kind, sheet_ref = excluded.sheet_ref",
                rows,
            )
        return len(rows)

    def news_stories_posted(self, *, since_iso: str) -> int:
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM news_stories WHERE posted_on >= ?",
                (str(since_iso),),
            ).fetchone()
        return int(row["n"] or 0)

    def news_stories_on(self, on_date: str) -> list[dict]:
        """Every story posted on one day, main and breaking, oldest first.

        R2 reads this: the screen is about the companies in the news the team
        was shown, not a second search.
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT url, title AS headline, about AS what, topic, importance, "
                "  kind, url_key, news_kind, sheet_ref FROM news_stories "
                "WHERE posted_on = ? "
                "ORDER BY created_at ASC, rowid ASC", (str(on_date),),
            ).fetchall()
        return [dict(r) for r in rows]

    def news_headlines_today(self, on_date: str) -> list[str]:
        """The day's posted headlines, main AND breaking, for the "do not return
        these" line of the next search. At most 25."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT title FROM news_stories WHERE posted_on = ? AND title <> '' "
                "ORDER BY created_at ASC, rowid ASC LIMIT 25", (str(on_date),),
            ).fetchall()
        return [str(r["title"]) for r in rows]

    def news_topic_count_today(self, topic: str, on_date: str) -> int:
        """How many INDUSTRY stories on `topic` went out on `on_date`, in the
        main post or a breaking one.

        NOT THE OVERFLOW POST AND NOT PoC NEWS. The per-topic limit shapes the
        main post; "More AI News" is where the stories it kept out go, so
        counting them would have the limit feed itself — and news about our own
        people is exempt from the limit, so it does not use it up either.

        NOT AN ANSWER EITHER (kind=answer). A story given to somebody who asked
        is remembered so it is not sent twice, but one person asking about a
        topic in the morning must not use up that topic's place in the post.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM news_stories WHERE posted_on = ? "
                "AND lower(topic) = lower(?) AND kind NOT IN ('overflow', 'answer') "
                "AND news_kind <> 'poc'", (str(on_date), str(topic or "")),
            ).fetchone()
        return int(row["n"] or 0)

    def news_topics_this_week(self, iso_week: str) -> list[str]:
        """The distinct topics posted in an ISO week ("2026-W40")."""
        try:
            year, week = str(iso_week).split("-W")
            monday = date.fromisocalendar(int(year), int(week), 1)
        except (ValueError, TypeError):
            log.warning("[news] %r is not an ISO week; no topics counted", iso_week)
            return []
        sunday = monday + timedelta(days=6)
        with self.conn() as c:
            rows = c.execute(
                "SELECT DISTINCT topic FROM news_stories WHERE posted_on BETWEEN ? AND ? "
                "AND topic <> '' AND kind NOT IN ('overflow', 'answer') "
                "AND news_kind <> 'poc'",
                (monday.isoformat(), sunday.isoformat()),
            ).fetchall()
        return [str(r["topic"]) for r in rows]

    # -- R1: the hourly checks ---------------------------------------------

    def news_check_done(self, on_date: str, slot: str) -> bool:
        """Has this check slot run on `on_date`? FAILS CLOSED — an unreadable
        table says yes, and the slot is skipped rather than searched twice."""
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM news_checks WHERE on_date = ? AND slot_hhmm = ?",
                    (str(on_date), str(slot)),
                ).fetchone()
            return row is not None
        except Exception:
            log.exception("[news-check] could not read news_checks; skipping the slot")
            return True

    def claim_news_check(self, on_date: str, slot: str, *, ran_at: str) -> bool:
        """Claim a check slot BEFORE searching. False when it was already taken,
        so two ticks — or a restart mid-search — cannot run it twice."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO news_checks (on_date, slot_hhmm, ran_at) "
                "VALUES (?, ?, ?)", (str(on_date), str(slot), str(ran_at)),
            )
            return cur.rowcount > 0

    def finish_news_check(self, on_date: str, slot: str, *, searches: int,
                          found: int, posted: int) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE news_checks SET searches = ?, found = ?, posted = ? "
                "WHERE on_date = ? AND slot_hhmm = ?",
                (int(searches), int(found), int(posted), str(on_date), str(slot)),
            )

    def clear_test_day(self, on_date: str) -> dict:
        """Forget ONE date's drip sends and news checks. {"drip_sends": n, "news_checks": n}.

        THE TEST DAY'S STALE-SEND CLEAR. A pretend date re-run a second time
        found its slots already taken by the first run and posted nothing.
        Strictly `WHERE on_date = ?` — no other date is ever touched; "reset
        test state" is the tool that wipes everything.
        """
        with self.conn() as c:
            sends = c.execute("DELETE FROM drip_sends WHERE on_date = ?",
                              (str(on_date),)).rowcount
            checks = c.execute("DELETE FROM news_checks WHERE on_date = ?",
                               (str(on_date),)).rowcount
        return {"drip_sends": int(sends or 0), "news_checks": int(checks or 0)}

    def news_breaking_messages_today(self, on_date: str) -> int:
        """Breaking MESSAGES sent on `on_date`: checks whose message went out."""
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM news_checks WHERE on_date = ? AND posted > 0",
                (str(on_date),),
            ).fetchone()
        return int(row["n"] or 0)

    def news_checks_on(self, on_date: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM news_checks WHERE on_date = ? ORDER BY slot_hhmm",
                (str(on_date),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- R3: discovered events ---------------------------------------------

    def event_discovery_seen(self, event_key: str) -> Optional[dict]:
        """Has this event already been proposed? The row, or None.

        A DECLINED PROPOSAL IS AN ANSWER. An event somebody said no to must not
        come back a fortnight later as a fresh discovery — that is not a
        reminder, it is nagging, and it teaches people to ignore the message.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT * FROM event_discoveries WHERE event_key = ?",
                    (str(event_key),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            log.exception("[events] could not read the discovery ledger")
            return {"status": "unknown"}          # fails closed: do not re-propose

    def record_event_discoveries(self, events: list, *, on_date: str,
                                 month: str) -> int:
        rows = [
            (str(e["event_key"]), str(e.get("name") or ""),
             str(e.get("event_date") or ""), str(e.get("location") or ""),
             str(e.get("link") or ""), str(on_date), str(month))
            for e in (events or []) if str(e.get("event_key") or "").strip()
        ]
        if not rows:
            return 0
        with self.conn() as c:
            cur = c.executemany(
                "INSERT OR IGNORE INTO event_discoveries "
                "(event_key, name, event_date, location, link, proposed_on, month) "
                "VALUES (?,?,?,?,?,?,?)", rows,
            )
            return max(0, cur.rowcount or 0)

    def set_event_discovery_status(self, event_key: str, status: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE event_discoveries SET status = ? WHERE event_key = ?",
                (str(status), str(event_key)),
            )

    # -- the per-day research cache ----------------------------------------

    @staticmethod
    def _cache_encode(value) -> str:
        """JSON with dates tagged, so R3's proposals come back as dates."""
        import json
        from datetime import date as _date

        def enc(v):
            if isinstance(v, _date):
                return {"__date__": v.isoformat()}
            raise TypeError(f"not JSON-serialisable: {type(v).__name__}")

        return json.dumps(value, default=enc)

    @staticmethod
    def _cache_decode(raw: str, empty):
        import json
        from datetime import date as _date, datetime as _datetime

        def dec(obj):
            if set(obj) == {"__date__"}:
                text = str(obj["__date__"])
                return (_datetime.fromisoformat(text) if "T" in text
                        else _date.fromisoformat(text))
            return obj

        try:
            return json.loads(raw or "", object_hook=dec)
        except (TypeError, ValueError):
            return empty

    def research_cache_get(self, item_key: str, *, on_date: str = "",
                           max_age_days: int = 0) -> Optional[dict]:
        """The cached research for one item, or None. FAILS OPEN, to a miss.

        TWO WAYS TO ASK. `max_age_days` — the per-row research (R6, R8, R10):
        the newest row for this ITEM stored within that many REAL days, whatever
        date it was stored under. `on_date` alone — the day's news run, which
        is about that day and nothing else.

        An unreadable cache costs one search, which the budget check still
        bounds; failing the other way would leave an item unresearched for the
        rest of the day.
        """
        try:
            with self.conn() as c:
                if int(max_age_days or 0) > 0:
                    row = c.execute(
                        "SELECT * FROM research_cache WHERE item_key = ? "
                        "AND created_at >= datetime('now', ?) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (str(item_key), f"-{int(max_age_days)} days"),
                    ).fetchone()
                else:
                    row = c.execute(
                        "SELECT * FROM research_cache WHERE item_key = ? "
                        "AND on_date = ?", (str(item_key), str(on_date)),
                    ).fetchone()
        except Exception:
            log.exception("[research-cache] could not read %s; treating it as a miss",
                          item_key)
            return None
        if row is None:
            return None
        return {
            "item_key": row["item_key"], "on_date": row["on_date"],
            "rule_id": row["rule_id"], "research": row["research"] or "",
            "sources": self._cache_decode(row["sources_json"], []),
            "note": row["note"] or "",
            "payload": self._cache_decode(row["payload_json"], {}),
        }

    def research_cache_put(self, item_key: str, *, on_date: str, rule_id: str = "",
                           research: str = "", sources: Optional[list] = None,
                           note: str = "", payload: Optional[dict] = None) -> bool:
        """Remember one item's research for today. Last write wins."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO research_cache (item_key, on_date, rule_id, research, "
                    "sources_json, note, payload_json) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(item_key, on_date) DO UPDATE SET "
                    "rule_id = excluded.rule_id, research = excluded.research, "
                    "sources_json = excluded.sources_json, note = excluded.note, "
                    "payload_json = excluded.payload_json, "
                    "created_at = CURRENT_TIMESTAMP",
                    (str(item_key), str(on_date), str(rule_id or ""),
                     str(research or ""), self._cache_encode(list(sources or [])),
                     str(note or ""), self._cache_encode(dict(payload or {}))),
                )
            return True
        except Exception:
            log.exception("[research-cache] could not store %s", item_key)
            return False

    def event_discoveries_this_month(self, month: str) -> int:
        """How many have been proposed in `month` (YYYY-MM). FAILS CLOSED HIGH.

        An unreadable table returns the cap, so discovery stays quiet rather
        than proposing without a limit.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM event_discoveries WHERE month = ?",
                    (str(month),),
                ).fetchone()
            return int(row["n"] or 0)
        except Exception:
            log.exception("[events] could not count this month's discoveries")
            return 10 ** 6

    # -- R3: registration deadlines we could not find ----------------------

    def deadline_recently_checked(self, event_key: str, *, since_iso: str) -> bool:
        """Did we already look for this deadline and fail, recently?

        FAILS CLOSED — an unreadable table means "yes, recently", so the bot
        stays quiet rather than asking about the same six events fortnightly.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM event_deadline_checks WHERE event_key = ? "
                    "AND last_checked >= ? LIMIT 1",
                    (str(event_key), str(since_iso)),
                ).fetchone()
            return row is not None
        except Exception:
            log.exception("[events] could not read the deadline-check ledger")
            return True

    def record_deadline_miss(self, *, event_key: str, name: str, on_date: str,
                             note: str = "") -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO event_deadline_checks "
                "(event_key, name, last_checked, note) VALUES (?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET "
                "  last_checked = excluded.last_checked, "
                "  attempts = event_deadline_checks.attempts + 1, "
                "  note = excluded.note",
                (str(event_key), str(name), str(on_date), str(note)[:300]),
            )

    def clear_deadline_miss(self, event_key: str) -> None:
        """The deadline turned up — stop remembering that it did not."""
        with self.conn() as c:
            c.execute("DELETE FROM event_deadline_checks WHERE event_key = ?",
                      (str(event_key),))

    # -- the voice profile (one row) ---------------------------------------

    @staticmethod
    def _json_or(raw, default):
        try:
            value = json.loads(raw or "")
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    def voice_row(self) -> dict:
        """The voice_profile row as a dict, JSON decoded. ALWAYS a dict: with no
        row every field is empty, and `built_at` == "" means "no profile".

        FAILS OPEN, at the empty profile. An unreadable row costs the learned
        tone for one message (the policy's hand-written exemplars are used
        instead), never the message.
        """
        empty = {"built_at": "", "lookback_days": 0, "message_count": 0,
                 "author_count": 0, "channels": [], "stats": {}, "exemplars": [],
                 "note": "", "note_source": "", "excluded_ids": []}
        try:
            with self.conn() as c:
                row = c.execute("SELECT * FROM voice_profile WHERE id = 1").fetchone()
        except Exception:
            log.exception("[voice] the voice_profile row could not be read")
            return empty
        if row is None:
            return empty
        return {
            "built_at": row["built_at"] or "",
            "lookback_days": int(row["lookback_days"] or 0),
            "message_count": int(row["message_count"] or 0),
            "author_count": int(row["author_count"] or 0),
            "channels": self._json_or(row["channels"], []),
            "stats": self._json_or(row["stats"], {}),
            "exemplars": self._json_or(row["exemplars"], []),
            "note": row["note"] or "",
            "note_source": row["note_source"] or "",
            "excluded_ids": [int(u) for u in self._json_or(row["excluded_ids"], [])
                             if str(u).lstrip("-").isdigit()],
        }

    def save_voice_profile(self, *, built_at: str, lookback_days: int,
                           message_count: int, author_count: int, channels: list,
                           stats: dict, exemplars: list, note: str,
                           note_source: str) -> None:
        """Replace the profile. `excluded_ids` is NOT touched — the opt-outs
        outlive every rebuild."""
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO voice_profile
                    (id, built_at, lookback_days, message_count, author_count,
                     channels, stats, exemplars, note, note_source)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    built_at = excluded.built_at,
                    lookback_days = excluded.lookback_days,
                    message_count = excluded.message_count,
                    author_count = excluded.author_count,
                    channels = excluded.channels, stats = excluded.stats,
                    exemplars = excluded.exemplars, note = excluded.note,
                    note_source = excluded.note_source
                """,
                (str(built_at), int(lookback_days), int(message_count),
                 int(author_count), json.dumps(list(channels or [])),
                 json.dumps(stats or {}, ensure_ascii=False),
                 json.dumps(list(exemplars or []), ensure_ascii=False),
                 str(note or ""), str(note_source or "")),
            )

    def clear_voice_profile(self) -> None:
        """Forget what was learned; keep who opted out."""
        with self.conn() as c:
            c.execute(
                "UPDATE voice_profile SET built_at = '', lookback_days = 0, "
                "message_count = 0, author_count = 0, channels = '[]', "
                "stats = '{}', exemplars = '[]', note = '', note_source = '' "
                "WHERE id = 1")

    def voice_exclude(self, user_id: int) -> int:
        """"Forget my messages": add `user_id` to the opt-outs and drop their
        stored examples NOW. How many examples went. The numbers and the note
        still reflect them until the rebuild the caller runs next."""
        uid = int(user_id)
        row = self.voice_row()
        excluded = sorted(set(row["excluded_ids"]) | {uid})
        kept = [e for e in row["exemplars"]
                if int((e or {}).get("author_id") or 0) != uid]
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO voice_profile (id) VALUES (1)")
            c.execute(
                "UPDATE voice_profile SET excluded_ids = ?, exemplars = ? WHERE id = 1",
                (json.dumps(excluded), json.dumps(kept, ensure_ascii=False)),
            )
        return len(row["exemplars"]) - len(kept)

    def company_names(self) -> list:
        """Every company the pipeline snapshot has ever seen (pipeline_companies)
        — names to keep OUT of the voice profile. [] when unreadable."""
        try:
            with self.conn() as c:
                rows = c.execute("SELECT company FROM pipeline_companies").fetchall()
            return [r["company"] for r in rows if r["company"]]
        except Exception:
            log.exception("[voice] the pipeline companies could not be read")
            return []

    def get_meta(self, key: str) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )

    # -- row mapping -------------------------------------------------------

    @staticmethod
    def _deadline_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "company": row["company"],
            "company_key": row["company_key"],
            "kind": row["kind"],
            "due_date": row["due_date"],
            "rule": row["rule"],
            "source": row["source"],
            "sheet_row": int(row["sheet_row"]) if row["sheet_row"] else None,
            "sheet_target": row["sheet_target"] or "",
            "sheet_cell": row["sheet_cell"] or "",
            "owner_id": int(row["owner_id"]) if row["owner_id"] else None,
            "owner_name": row["owner_name"] or "",
            "channel_id": int(row["channel_id"]) if row["channel_id"] else None,
            "announced": bool(row["announced"]),
            "reminded": bool(row["reminded"]),
            "chases_sent": int(row["chases_sent"] or 0),
            "escalated": bool(row["escalated"]),
            "status": row["status"],
        }

    @staticmethod
    def _chase_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "channel_id": int(row["channel_id"]),
            "message_id": int(row["message_id"]),
            "jump_url": row["jump_url"] or "",
            "person_id": int(row["person_id"]) if row["person_id"] else None,
            "person_name": row["person_name"],
            "what": row["what"],
            "promised_at": row["promised_at"],
            "due_at": row["due_at"],
            "status": row["status"],
            "reminders_sent": int(row["reminders_sent"] or 0),
            "last_reminder_id": (
                int(row["last_reminder_id"]) if row["last_reminder_id"] else None
            ),
            "last_reminded_at": row["last_reminded_at"],
            "flagged": bool(row["flagged"]),
        }
