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
import logging
import re
import sqlite3
from contextlib import contextmanager
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
        # One naive, one aware. Compare them as naive rather than raising: both
        # are written by this bot in IST, and refusing the undo over a tzinfo
        # mismatch would be a strange thing to explain to somebody.
        a = a.replace(tzinfo=None)
        b = b.replace(tzinfo=None)
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
    slot         INTEGER NOT NULL,   -- 1..DAILY_MESSAGE_CAP, the day's send slot
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
"""


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` won't
# alter an existing table, so an already-created sales_bot.db needs them
# back-filled. (table, column, DDL) — each added only if missing. Idempotent.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # THE PENDING RECORD-OFFER. When suppress-or-convert turns a nudge into
    # "want me to mark it on the row?", the fields it offered are stored on the
    # drip row — so a reply of "yes" has something concrete to apply. Added
    # after drip_sends first shipped, hence the migration.
    ("drip_sends", "offer", "TEXT NOT NULL DEFAULT ''"),
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
        on_date: str = "",
    ) -> int:
        """Record a one-off reminder somebody asked for at a specific time.

        Returns its id. These are the ONLY dates exempt from the weekend shift —
        see the table comment in SCHEMA.
        """
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO scheduled_reminders (row_key, company, poc, due_date, "
                "due_time, what, requested_by, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (str(row_key or ""), str(company or ""), str(poc or ""), str(due_date),
                 str(due_time or ""), str(what), str(requested_by or ""),
                 str(on_date or "")),
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

    def list_scheduled_reminders(self, limit: int = 200) -> list[dict]:
        """Every OPEN reminder, soonest first — including ones not tied to a row."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, row_key, company, poc, due_date, due_time, what, "
                "requested_by, created_at FROM scheduled_reminders "
                "WHERE status = 'open' ORDER BY due_date ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

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
                "companies, stage, planned_at, sent_at FROM drip_sends "
                "WHERE on_date = ? ORDER BY slot ASC",
                (str(on_date),),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_drip_send(
        self, *, on_date: str, slot: int, group_key: str, action_type: str,
        owner_key: str = "", owner_label: str = "", companies: str = "",
        stage: str = "nudge", planned_at: str = "", channel_id=None,
        message_id=None, sent_at: str = "",
    ) -> bool:
        """Record that one drip message went out. False when that slot was
        already taken.

        The UNIQUE (on_date, slot) constraint is doing real work: two sweep
        ticks racing on the same slot both try to insert, one wins, and the
        loser does not send. It is cheaper and more certain than a lock.
        """
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO drip_sends (on_date, slot, group_key, action_type, "
                    "owner_key, owner_label, companies, stage, planned_at, channel_id, "
                    "message_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(on_date), int(slot), str(group_key), str(action_type),
                     str(owner_key or ""), str(owner_label or ""), str(companies or ""),
                     str(stage or "nudge"), str(planned_at or ""),
                     str(channel_id) if channel_id else None,
                     str(message_id) if message_id else None, str(sent_at or "")),
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
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT group_key, stage, MAX(on_date) AS last_date FROM drip_sends "
                    "WHERE on_date >= date('now', ?) GROUP BY group_key, stage",
                    (f"-{max(1, int(lookback_days))} day",),
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

    def attach_drip_message_id(self, *, on_date: str, slot: int, message_id) -> bool:
        """Record which Discord message a drip slot became.

        The slot row is written BEFORE the send (that is what claims it against
        a racing tick), so the message id can only be filled in afterwards. It
        matters because a REPLY to a drip message is one of the two things that
        may write to the sheet, and `find_drip_by_message_id` is how the reply
        finds out which companies that nudge was about.
        """
        with self.conn() as c:
            cur = c.execute(
                "UPDATE drip_sends SET message_id = ? WHERE on_date = ? AND slot = ?",
                (str(message_id), str(on_date), int(slot)),
            )
        return bool(cur.rowcount)

    def find_drip_by_message_id(self, message_id: str) -> Optional[dict]:
        """The drip row a Discord message id belongs to, or None.

        THE CONTEXT FOR A REPLY. "Sent this morning" names no company and no
        column; the nudge it answers names both, and this is the lookup that
        connects them. None simply means the reply was to something else the bot
        said, and the extractor works from the reply alone.
        """
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT on_date, slot, group_key, action_type, owner_label, "
                    "companies, stage, offer FROM drip_sends WHERE message_id = ?",
                    (str(message_id),),
                ).fetchone()
        except Exception:
            log.exception("[db] could not look up the drip message %r", message_id)
            return None
        return dict(row) if row else None

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
