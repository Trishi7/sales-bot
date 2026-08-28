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
    row_key     TEXT PRIMARY KEY,   -- "<company>|<poc>" from the master tab
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

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` won't
# alter an existing table, so an already-created sales_bot.db needs them
# back-filled. (table, column, DDL) — each added only if missing. Idempotent.
_MIGRATIONS: list[tuple[str, str, str]] = []


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
